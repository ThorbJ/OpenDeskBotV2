"""Virtual device link: a robot running in a web page dials in over local WebSocket.

A "virtual device" is the unified firmware running on virtual hardware inside a
browser tab (for example a WebAssembly build of ``hardware/firmware``).  It has
no USB port, so the page carries the device's raw DBOT v1 byte stream over a
WebSocket to this PC instead.  Every binary message is a slice of that byte
stream, in both directions; the protocol bytes are exactly those of USB CDC.

Each accepted connection is wrapped in :class:`WebSocketSerial` — a blocking
``SerialLike`` — and adopted by :meth:`SerialDeviceManager.attach_stream_session`,
so it runs the same hello / heartbeat / PB machinery as a COM port and, once the
DBOT hello completes, is the same device session the rest of Core sees.

Trust boundary:

- Off by default (``DESKBOT_VIRTUAL_DEVICE_ENABLED``); the console toggles it
  and Core applies the change within about a second, no restart.
- Binds 127.0.0.1 only.  There is deliberately no host setting.
- Only web pages whose ``Origin`` is in the allow-list may connect
  (``DESKBOT_VIRTUAL_DEVICE_ORIGINS``); a missing Origin is rejected too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any, Callable, Iterable

from deskbot_server.env import load_dotenv, update_env_keys

logger = logging.getLogger("deskbot-server")

VIRTUAL_DEVICE_HOST = "127.0.0.1"
VIRTUAL_DEVICE_PATH = "/virtual-device"
VIRTUAL_DEVICE_TRANSPORT = "virtual_ws"
DEFAULT_VIRTUAL_DEVICE_PORT = 9022
# ``:*`` matches any port, for local development servers.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://thorb.com",
    "http://localhost:*",
    "http://127.0.0.1:*",
)
ENABLED_ENV = "DESKBOT_VIRTUAL_DEVICE_ENABLED"
PORT_ENV = "DESKBOT_VIRTUAL_DEVICE_PORT"
ORIGINS_ENV = "DESKBOT_VIRTUAL_DEVICE_ORIGINS"

# recv timeout doubles as the session reader thread's stop-check interval.
_READ_TIMEOUT = 0.05
_WRITE_TIMEOUT = 5.0
# One DBOT frame is far smaller; this only bounds a misbehaving page.
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def virtual_device_enabled() -> bool:
    raw = os.environ.get(ENABLED_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def virtual_device_port() -> int:
    try:
        port = int(os.environ.get(PORT_ENV) or DEFAULT_VIRTUAL_DEVICE_PORT)
    except ValueError:
        return DEFAULT_VIRTUAL_DEVICE_PORT
    return port if 1 <= port <= 65535 else DEFAULT_VIRTUAL_DEVICE_PORT


def parse_origins(raw: str | None) -> tuple[str, ...]:
    """Split a comma / whitespace separated list; empty means the defaults."""
    parts = [
        part.strip().rstrip("/")
        for part in str(raw or "").replace(",", " ").split()
        if part.strip()
    ]
    return tuple(dict.fromkeys(parts)) or DEFAULT_ALLOWED_ORIGINS


def virtual_device_allowed_origins() -> tuple[str, ...]:
    return parse_origins(os.environ.get(ORIGINS_ENV))


def origin_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    """Exact ``scheme://host[:port]`` match; an entry ending in ``:*`` allows any port."""
    value = str(origin or "").strip().lower().rstrip("/")
    if not value or value == "null":
        return False
    for entry in allowed:
        rule = str(entry or "").strip().lower().rstrip("/")
        if not rule:
            continue
        if rule.endswith(":*"):
            base = rule[:-2]
            if value == base:
                return True
            if value.startswith(base + ":") and value[len(base) + 1 :].isdigit():
                return True
        elif value == rule:
            return True
    return False


_ORIGIN_RULE = re.compile(
    r"^https?://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:(\d{1,5}|\*))?$",
    re.IGNORECASE,
)
_ENV_COMMENT = "# 虚拟设备连接（本机 WebSocket，只听 127.0.0.1）"


def virtual_device_settings() -> dict[str, Any]:
    """Console view of the setting (re-reads ``.env``)."""
    load_dotenv()
    port = virtual_device_port()
    return {
        "enabled": virtual_device_enabled(),
        "host": VIRTUAL_DEVICE_HOST,
        "port": port,
        "path": VIRTUAL_DEVICE_PATH,
        "url": f"ws://{VIRTUAL_DEVICE_HOST}:{port}{VIRTUAL_DEVICE_PATH}",
        "origins": list(virtual_device_allowed_origins()),
        "default_origins": list(DEFAULT_ALLOWED_ORIGINS),
    }


def save_virtual_device_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Write ``enabled`` and/or ``origins`` (blank = defaults); Core applies it within ~1 s.

    Raises ``ValueError`` naming the first malformed origin; nothing is written then.
    """
    updates: dict[str, str] = {}
    explicit_empty: set[str] = set()
    if "enabled" in payload:
        updates[ENABLED_ENV] = "1" if bool(payload.get("enabled")) else "0"
    if "origins" in payload:
        raw = payload.get("origins")
        if isinstance(raw, (list, tuple)):
            raw = " ".join(str(item) for item in raw)
        entries = [
            part.strip().rstrip("/")
            for part in str(raw or "").replace(",", " ").split()
            if part.strip()
        ]
        for entry in entries:
            if not _ORIGIN_RULE.match(entry):
                raise ValueError(
                    f"来源格式不对：{entry}（应为 https://example.com 或 http://localhost:*）"
                )
        updates[ORIGINS_ENV] = ",".join(dict.fromkeys(entries))
        if not entries:
            explicit_empty.add(ORIGINS_ENV)
    if updates:
        update_env_keys(
            updates,
            keys=(ENABLED_ENV, ORIGINS_ENV),
            comment=_ENV_COMMENT,
            write_empty_keys=explicit_empty,
        )
    return virtual_device_settings()


class WebSocketSerial:
    """Adapt one WebSocket connection to the blocking ``SerialLike`` shape.

    The session reads from its own thread and writes from worker threads, so
    inbound messages land in a condition-guarded buffer and writes hop onto the
    event loop.  Like :class:`SocketSerial`, ``read`` returns ``b""`` on timeout
    and raises ``OSError`` once the page has gone, so the session closes.
    """

    def __init__(self, websocket: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = websocket
        self._loop = loop
        self._condition = threading.Condition()
        self._rx = bytearray()
        self._peer_gone = False
        self._closed = False
        self.timeout: float | None = _READ_TIMEOUT
        self.write_timeout: float | None = _WRITE_TIMEOUT

    # -- called on the event loop by the connection handler -----------------
    def feed(self, data: bytes) -> None:
        with self._condition:
            self._rx.extend(data)
            self._condition.notify_all()

    def peer_closed(self) -> None:
        with self._condition:
            self._peer_gone = True
            self._condition.notify_all()

    # -- SerialLike ---------------------------------------------------------
    def read(self, size: int = 1) -> bytes:
        with self._condition:
            if not self._rx and not self._peer_gone and not self._closed:
                self._condition.wait(self.timeout or _READ_TIMEOUT)
            if self._rx:
                count = min(max(1, int(size)), len(self._rx))
                data = bytes(self._rx[:count])
                del self._rx[:count]
                return data
            if self._closed:
                return b""
            if self._peer_gone:
                raise OSError("virtual device page closed the connection")
            return b""

    def write(self, data: bytes) -> int:
        if self._closed or self._peer_gone:
            raise OSError("virtual device link is closed")
        payload = bytes(data)
        future = asyncio.run_coroutine_threadsafe(self._ws.send(payload), self._loop)
        try:
            future.result(timeout=self.write_timeout or _WRITE_TIMEOUT)
        except Exception as exc:
            future.cancel()
            raise OSError(f"virtual device write failed: {exc}") from exc
        return len(payload)

    def reset_input_buffer(self) -> None:
        """每次连接都是新字节流，没有上一次会话的残留要丢。"""
        return None

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._ws.close())
            )
        except RuntimeError:  # loop already closed during shutdown
            pass


class VirtualDeviceListener:
    """Accept virtual device pages on 127.0.0.1 and hand them to the serial manager."""

    def __init__(
        self,
        attach: Callable[..., Any],
        *,
        port: int | None = None,
        allowed_origins: Iterable[str] | Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._attach = attach
        self._requested_port = virtual_device_port() if port is None else int(port)
        if allowed_origins is None:
            self._origins: Callable[[], Iterable[str]] = virtual_device_allowed_origins
        elif callable(allowed_origins):
            self._origins = allowed_origins
        else:
            fixed = tuple(allowed_origins)
            self._origins = lambda: fixed
        self._server: Any = None

    @property
    def host(self) -> str:
        return VIRTUAL_DEVICE_HOST

    @property
    def port(self) -> int:
        """The bound port (the requested one until :meth:`start`)."""
        addresses = self.addresses
        return addresses[0][1] if addresses else self._requested_port

    @property
    def addresses(self) -> list[tuple[str, int]]:
        server = self._server
        if server is None:
            return []
        return [
            (str(sock.getsockname()[0]), int(sock.getsockname()[1]))
            for sock in server.sockets
        ]

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"ws://{VIRTUAL_DEVICE_HOST}:{self.port}{VIRTUAL_DEVICE_PATH}"

    async def start(self) -> bool:
        if self._server is not None:
            return True
        from websockets.asyncio.server import serve

        try:
            self._server = await serve(
                self._handle,
                VIRTUAL_DEVICE_HOST,
                self._requested_port,
                process_request=self._check_request,
                max_size=_MAX_MESSAGE_BYTES,
                compression=None,
            )
        except OSError as exc:
            logger.warning(
                "[virtual_device] cannot listen on %s:%d err=%s; virtual device link off",
                VIRTUAL_DEVICE_HOST,
                self._requested_port,
                exc,
            )
            self._server = None
            return False
        logger.info("[virtual_device] listening on %s", self.url)
        return True

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()
        logger.info("[virtual_device] stopped")

    def _check_request(self, connection, request):
        path = str(request.path or "").split("?", 1)[0]
        if path != VIRTUAL_DEVICE_PATH:
            return connection.respond(404, "Not Found\n")
        origin = request.headers.get("Origin")
        if not origin_allowed(origin, self._origins()):
            logger.warning("[virtual_device] rejected origin=%r", origin)
            return connection.respond(403, "Origin not allowed\n")
        return None

    async def _handle(self, websocket) -> None:
        loop = asyncio.get_running_loop()
        adapter = WebSocketSerial(websocket, loop)
        peer = websocket.remote_address or ("127.0.0.1", 0)
        label = f"virtual://{peer[0]}:{peer[1]}"
        try:
            await self._attach(label, adapter, transport=VIRTUAL_DEVICE_TRANSPORT)
        except Exception as exc:
            logger.warning("[virtual_device] attach failed peer=%s err=%s", label, exc)
            adapter.close()
            return
        try:
            async for message in websocket:
                if isinstance(message, str):
                    # DBOT is a byte stream; text frames are a page bug.
                    await websocket.close(1003, "binary DBOT frames only")
                    break
                adapter.feed(message)
        except Exception:  # noqa: BLE001 - abrupt close ends the link like unplugging
            logger.debug("[virtual_device] connection ended", exc_info=True)
        finally:
            adapter.peer_closed()


class VirtualDeviceLink:
    """Start or stop the listener to follow the setting, without restarting Core."""

    def __init__(
        self,
        attach: Callable[..., Any],
        *,
        listener_factory: Callable[..., VirtualDeviceListener] | None = None,
    ) -> None:
        self._attach = attach
        self._factory = listener_factory or (
            lambda attach, port: VirtualDeviceListener(attach, port=port)
        )
        self._listener: VirtualDeviceListener | None = None
        self._port: int | None = None

    @property
    def listener(self) -> VirtualDeviceListener | None:
        return self._listener

    async def sync(self) -> None:
        """Apply the current setting once (re-reading ``.env``)."""
        load_dotenv()
        enabled = virtual_device_enabled()
        port = virtual_device_port()
        if self._listener is not None and (not enabled or port != self._port):
            listener, self._listener = self._listener, None
            await listener.stop()
        if enabled and self._listener is None:
            listener = self._factory(self._attach, port)
            if await listener.start():
                self._listener = listener
            self._port = port

    async def run(self, *, interval_seconds: float = 1.0) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad setting must not stop Core
                logger.warning("[virtual_device] applying setting failed", exc_info=True)
            await asyncio.sleep(interval_seconds)

    async def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            await listener.stop()
