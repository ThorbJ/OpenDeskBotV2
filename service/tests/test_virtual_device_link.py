"""虚拟设备连接：浏览器里的虚拟设备经本机 WebSocket 接入，与 USB 走同一条 DBOT 会话。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from deskbot_server.infrastructure.serial.protocol import (
    Channel,
    FrameDecoder,
    FrameFlag,
    decode_control_payload,
    encode_control_payload,
    encode_frame,
)

DEVICE_ID = "deskbot_02464d534e01"
THORB = "https://thorb.com"


def _manager():
    from deskbot_server.infrastructure.serial.manager import (
        SerialDeviceManager,
        SerialManagerConfig,
    )

    # USB 扫描关掉：测试只看虚拟设备这一条链路，绝不碰开发机的真实串口。
    return SerialDeviceManager(SerialManagerConfig(enabled=False, hello_timeout=3.0))


def _hello_ack(
    nonce: int, *, epoch: int = 7, sequence: int = 1, timeout_ms: int = 6000
) -> bytes:
    return encode_frame(
        Channel.CONTROL_JSON,
        encode_control_payload(
            {
                "type": "hello_ack",
                "protocol": 1,
                "ack_client_nonce": nonce,
                "device_id": DEVICE_ID,
                "product": "Deskbot",
                "firmware": "0.0.59",
                "session_epoch": epoch,
                "heartbeat_ms": 1000,
                "timeout_ms": timeout_ms,
                "max_payload": 1024 * 1024,
                "capabilities": [
                    "control_json",
                    "pb_wire",
                    "audio_up_opus",
                    "audio_down_opus",
                    "camera_jpeg",
                    "frame_ack",
                    "pb_json_fragments",
                ],
            }
        ),
        sequence=sequence,
        session_epoch=epoch,
        flags=FrameFlag.JSON,
    )


async def _simulated_device(
    ws,
    *,
    stop: asyncio.Event,
    timeout_ms: int = 6000,
    pb_ack_delay: float | None = 0.0,
) -> None:
    """最小的"固件"：回 hello_ack，对要求确认的帧回 frame_ack。

    ``pb_ack_delay``：PB 帧的确认晚这么多秒才发出，期间页面一个字节也不发
    （后台标签页拿不到定时器时就是这样）；``None`` 表示永远不确认。
    """
    decoder = FrameDecoder()
    sequence = 1
    while not stop.is_set():
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=0.1)
        except TimeoutError:
            continue
        except ConnectionClosed:
            return
        for frame in decoder.feed(message):
            if frame.channel == Channel.PB_WIRE:
                if pb_ack_delay is None:
                    continue
                await asyncio.sleep(pb_ack_delay)
            elif frame.channel != Channel.CONTROL_JSON:
                continue
            sequence += 1
            if frame.flags & FrameFlag.ACK_REQUIRED:
                await ws.send(
                    encode_frame(
                        Channel.CONTROL_JSON,
                        encode_control_payload(
                            {
                                "type": "frame_ack",
                                "session_epoch": frame.session_epoch,
                                "ack_sequence": frame.sequence,
                                "ack_channel": int(frame.channel),
                            }
                        ),
                        sequence=sequence,
                        session_epoch=frame.session_epoch,
                        flags=FrameFlag.ACK,
                    )
                )
                continue
            payload = decode_control_payload(frame.payload)
            if payload.get("type") == "hello":
                await ws.send(
                    _hello_ack(
                        int(payload["client_nonce"]),
                        sequence=sequence,
                        timeout_ms=timeout_ms,
                    )
                )


async def _eventually(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def test_virtual_device_completes_dbot_hello_and_is_a_connected_device():
    from deskbot_server.infrastructure.net.virtual_device_link import (
        VIRTUAL_DEVICE_PATH,
        VirtualDeviceListener,
    )

    async def scenario():
        manager = _manager()
        listener = VirtualDeviceListener(
            manager.attach_stream_session, port=0, allowed_origins=(THORB,)
        )
        assert await listener.start()
        stop = asyncio.Event()
        try:
            url = f"ws://127.0.0.1:{listener.port}{VIRTUAL_DEVICE_PATH}"
            async with connect(url, origin=THORB) as ws:
                device = asyncio.create_task(_simulated_device(ws, stop=stop))
                await _eventually(lambda: manager.session_for_device(DEVICE_ID) is not None)
                session = manager.session_for_device(DEVICE_ID)
                assert session.transport == "virtual_ws"
                assert session.hello_info.firmware == "0.0.59"
                stop.set()
                await device
            # 页面关掉 = 拔线：会话随之关闭，设备不再算已连接。
            await _eventually(lambda: manager.session_for_device(DEVICE_ID) is None)
        finally:
            await listener.stop()
            await manager.stop()

    asyncio.run(scenario())


async def _connected_virtual_device(manager, listener, **device):
    url = f"ws://127.0.0.1:{listener.port}/virtual-device"
    ws = await connect(url, origin=THORB)
    stop = asyncio.Event()
    task = asyncio.create_task(_simulated_device(ws, stop=stop, **device))
    await _eventually(lambda: manager.session_for_device(DEVICE_ID) is not None)
    return ws, stop, task, manager.session_for_device(DEVICE_ID)


def _pb_wire(req: str) -> str:
    return json.dumps({"type": "pb_single", "req": req, "idx": 0, "chunk_ms": 1})


def test_a_page_in_the_background_slows_a_pb_down_without_disconnecting():
    """页面转到后台：设备变慢，不报错。

    后台标签页大约每秒才拿到一次定时器，有时十几秒一次也没有，这期间帧确认
    出不来。只要还在设备自己声明的存活时限（hello_ack timeout_ms）内，发 PB
    的一方就该等，而不是用自己更短的发送超时把整条会话关掉。
    """
    from deskbot_server.infrastructure.net.virtual_device_link import (
        VirtualDeviceListener,
    )
    from deskbot_server.ws.ws_send import _safe_send

    async def scenario():
        manager = _manager()
        listener = VirtualDeviceListener(
            manager.attach_stream_session, port=0, allowed_origins=(THORB,)
        )
        assert await listener.start()
        try:
            ws, stop, device, session = await _connected_virtual_device(
                manager, listener, pb_ack_delay=1.5
            )
            # The caller's send budget (10 s in production) is shorter than
            # the page's stall here, just as on 2026-11-17.
            assert await _safe_send(session, _pb_wire("slow-page"), timeout=0.5)
            assert not session.is_closed
            assert manager.session_for_device(DEVICE_ID) is session
            stop.set()
            await device
            await ws.close()
        finally:
            await listener.stop()
            await manager.stop()

    asyncio.run(scenario())


def test_a_page_that_stops_answering_is_still_dropped_by_the_heartbeat():
    from deskbot_server.infrastructure.net.virtual_device_link import (
        VirtualDeviceListener,
    )
    from deskbot_server.ws.ws_send import _safe_send

    async def scenario():
        manager = _manager()
        listener = VirtualDeviceListener(
            manager.attach_stream_session, port=0, allowed_origins=(THORB,)
        )
        assert await listener.start()
        try:
            ws, stop, device, session = await _connected_virtual_device(
                manager, listener, timeout_ms=1500, pb_ack_delay=None
            )
            started = time.monotonic()
            assert not await _safe_send(session, _pb_wire("dead-page"))
            # Released by the session's own liveness timeout, not by waiting
            # out the longer send budget.
            assert time.monotonic() - started < 5.0
            assert session.is_closed
            stop.set()
            await device
            await ws.close()
        finally:
            await listener.stop()
            await manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://thorb.com.evil.example",
        "http://thorb.com",
        None,
    ],
)
def test_pages_from_origins_outside_the_allow_list_are_refused(origin):
    from deskbot_server.infrastructure.net.virtual_device_link import (
        VIRTUAL_DEVICE_PATH,
        VirtualDeviceListener,
    )

    async def scenario():
        attached = []

        async def attach(*args, **kwargs):
            attached.append(args)

        listener = VirtualDeviceListener(attach, port=0, allowed_origins=(THORB,))
        assert await listener.start()
        try:
            url = f"ws://127.0.0.1:{listener.port}{VIRTUAL_DEVICE_PATH}"
            with pytest.raises(InvalidStatus) as refused:
                async with connect(url, origin=origin):
                    pass
            assert refused.value.response.status_code == 403
            assert attached == []
        finally:
            await listener.stop()

    asyncio.run(scenario())


def _connectable(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture()
def no_env_file(monkeypatch, tmp_path):
    """设置只来自本测试设定的环境变量，不读开发机的 .env。"""
    monkeypatch.setattr("deskbot_server.env.ENV_FILE", tmp_path / ".env")
    for name in (
        "DESKBOT_VIRTUAL_DEVICE_ENABLED",
        "DESKBOT_VIRTUAL_DEVICE_PORT",
        "DESKBOT_VIRTUAL_DEVICE_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_link_is_off_by_default_and_follows_the_setting_without_restart(
    monkeypatch, no_env_file
):
    from deskbot_server.infrastructure.net.virtual_device_link import (
        VirtualDeviceLink,
    )

    async def attach(*args, **kwargs):
        return None

    async def scenario():
        port = _free_port()
        monkeypatch.setenv("DESKBOT_VIRTUAL_DEVICE_PORT", str(port))
        link = VirtualDeviceLink(attach)
        try:
            await link.sync()
            assert link.listener is None
            assert not _connectable(port)

            monkeypatch.setenv("DESKBOT_VIRTUAL_DEVICE_ENABLED", "1")
            await link.sync()
            assert link.listener is not None
            assert _connectable(port)

            monkeypatch.setenv("DESKBOT_VIRTUAL_DEVICE_ENABLED", "0")
            await link.sync()
            assert link.listener is None
            assert not _connectable(port)
        finally:
            await link.stop()

    asyncio.run(scenario())


def test_listener_binds_the_loopback_address_only():
    import socket

    from deskbot_server.infrastructure.net.virtual_device_link import (
        VirtualDeviceListener,
    )

    async def attach(*args, **kwargs):
        return None

    async def scenario():
        listener = VirtualDeviceListener(attach, port=0, allowed_origins=(THORB,))
        assert await listener.start()
        try:
            assert listener.addresses
            assert {host for host, _port in listener.addresses} == {"127.0.0.1"}
            # 从本机的局域网地址连不上（有局域网地址时才能验这一条）。
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.connect(("192.0.2.1", 80))  # TEST-NET，不发包
                lan_ip = probe.getsockname()[0]
                probe.close()
            except OSError:
                lan_ip = ""
            if lan_ip and not lan_ip.startswith("127."):
                with pytest.raises(OSError):
                    socket.create_connection((lan_ip, listener.port), timeout=0.5).close()
        finally:
            await listener.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://thorb.com", True),
        ("https://thorb.com/", True),
        ("http://localhost:4321", True),
        ("http://127.0.0.1:8788", True),
        ("http://localhost", True),
        ("https://www.thorb.com", False),
        ("https://thorb.com:8443", False),
        ("http://localhost.evil.example:4321", False),
        ("http://127.0.0.1:80abc", False),
        ("null", False),
        ("", False),
    ],
)
def test_default_allow_list_is_thorb_and_local_dev_pages(no_env_file, origin, allowed):
    from deskbot_server.infrastructure.net.virtual_device_link import (
        origin_allowed,
        virtual_device_allowed_origins,
    )

    assert origin_allowed(origin, virtual_device_allowed_origins()) is allowed


def test_configured_allow_list_replaces_the_defaults(monkeypatch, no_env_file):
    from deskbot_server.infrastructure.net.virtual_device_link import (
        origin_allowed,
        virtual_device_allowed_origins,
    )

    monkeypatch.setenv(
        "DESKBOT_VIRTUAL_DEVICE_ORIGINS",
        "https://staging.example.com, http://localhost:5173",
    )
    origins = virtual_device_allowed_origins()
    assert origin_allowed("https://staging.example.com", origins)
    assert origin_allowed("http://localhost:5173", origins)
    assert not origin_allowed("http://localhost:4321", origins)
    assert not origin_allowed("https://thorb.com", origins)
