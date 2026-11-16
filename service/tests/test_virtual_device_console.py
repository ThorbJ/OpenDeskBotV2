"""控制台「设备连接」页的虚拟设备开关：写 .env、校验来源、页面入口与连接方式名称。"""

from __future__ import annotations

import pytest

from tests.test_app2c_pages import local_client  # noqa: F401 - pytest fixture


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    from deskbot_server import env as dotenv_module

    for key in (
        "DESKBOT_VIRTUAL_DEVICE_ENABLED",
        "DESKBOT_VIRTUAL_DEVICE_PORT",
        "DESKBOT_VIRTUAL_DEVICE_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(dotenv_module, "ENV_FILE", path)
    monkeypatch.setattr(dotenv_module, "_last_signature", None)
    monkeypatch.setattr(dotenv_module, "_file_managed_values", {})
    return path


def test_switch_is_off_by_default_and_saving_writes_the_env_file(local_client, env_file):
    client, _app = local_client

    r = client.get("/api/setup/virtual-device")
    body = r.get_json()
    assert r.status_code == 200
    assert body["enabled"] is False
    assert body["url"] == "ws://127.0.0.1:9022/virtual-device"
    assert body["origins"] == ["https://thorb.com", "http://localhost:*", "http://127.0.0.1:*"]

    r = client.patch("/api/setup/virtual-device", json={"enabled": True})
    assert r.status_code == 200 and r.get_json()["enabled"] is True
    assert "DESKBOT_VIRTUAL_DEVICE_ENABLED=1" in env_file.read_text(encoding="utf-8")

    r = client.patch(
        "/api/setup/virtual-device",
        json={"origins": "https://staging.example.com\nhttp://localhost:5173"},
    )
    assert r.get_json()["origins"] == ["https://staging.example.com", "http://localhost:5173"]

    # 清空 = 回到默认列表
    r = client.patch("/api/setup/virtual-device", json={"origins": ""})
    assert r.get_json()["origins"][0] == "https://thorb.com"

    r = client.patch("/api/setup/virtual-device", json={"enabled": False})
    assert r.get_json()["enabled"] is False
    assert "DESKBOT_VIRTUAL_DEVICE_ENABLED=0" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["*", "thorb.com", "ftp://thorb.com", "https://thorb.com/path"])
def test_malformed_origins_are_refused_with_a_reason(local_client, env_file, bad):
    client, _app = local_client
    r = client.patch("/api/setup/virtual-device", json={"origins": bad})
    assert r.status_code == 400
    assert bad in r.get_json()["error"]
    assert "DESKBOT_VIRTUAL_DEVICE_ORIGINS" not in (
        env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    )


def test_connection_page_offers_the_switch_and_names_the_transport(local_client, env_file):
    client, _app = local_client
    html = client.get("/devices").get_data(as_text=True)
    assert ">虚拟设备连接</div>" in html
    assert "/api/setup/virtual-device" in html
    assert "d.transport==='virtual_ws'" in html
