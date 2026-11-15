"""Connection through real loopback SSH, shell and files, without a real router."""

from __future__ import annotations

import json
import os

import pytest
from router_shell import ShellRouter
from ssh_test_server import loopback_ssh_client

from singroute.application.router_client import RouterFileReadError
from singroute.application.router_connection import inspect_router
from singroute.application.router_update import read_router_outbound_summary
from singroute.infrastructure.ssh_router import (
    HostKeyMismatchError,
    SshRouterError,
    UnknownHostKeyError,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.mark.parametrize(
    "installed,missing,server_auth",
    [
        (set(), "", "none"),
        ({"xray"}, "", "password"),
        ({"sing-box"}, "sing-box", "password"),
    ],
)
def test_gui_connects_without_ready_engine_over_real_ssh(
    make_window, tmp_path, monkeypatch, installed, missing, server_auth
):
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from singroute.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "_check_startup_update_when_idle", lambda _: None)
    window = make_window()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    router = ShellRouter(tmp_path / "router", installed, missing)
    try:
        with loopback_ssh_client(
            router,
            server_auth=server_auth,
            client_options={
                "password": "test-password" if server_auth == "password" else None
            },
            connect=False,
        ) as (client, progress):
            monkeypatch.setattr(window, "_new_client", lambda _: client)
            window._connect_router()
            loop = QEventLoop()
            poll = QTimer()
            poll.timeout.connect(lambda: loop.quit() if not window._busy else None)
            poll.start(10)
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(20000)
            loop.exec()
            poll.stop()
            deadline.stop()
            assert not window._busy
            assert not errors
            assert window._connected_client is client
            assert window.connection_state_label.text() == "Подключено"
            assert window.connect_button.text() == "Отключиться"
            assert window.engine_controls.isHidden()
            assert not window.apply_button.isEnabled()
            assert {e.name for e in window._router_info.engines} == installed
            text = window.old_preview.toPlainText()
            if not installed:
                assert "Установите пакет sing-box или xray-core" in text
            elif missing:
                assert "конфиг" in text.lower() or "прочитать" in text
                assert "Подключено" in window.status_label.text()
            else:
                assert window._target_engine in installed
                assert "router.test" in text
            assert "test-secret" not in window.log_edit.toPlainText()
            assert "test-secret" not in "\n".join(progress)
            # A failed cat must leave the real SSH session usable.
            assert client.run("cat /etc/openwrt_release").exit_code == 0
            if "sing-box" not in installed:
                assert "cat /etc/sing-box/config.json" not in router.calls
            if "xray" not in installed:
                assert "cat /etc/xray/config.json" not in router.calls
    finally:
        window._disconnect_router()
        app.processEvents()


@pytest.mark.parametrize("engine", ["sing-box", "xray"])
def test_auto_detected_custom_config_is_read_over_real_ssh(tmp_path, engine):
    router = ShellRouter(tmp_path / "router", {engine})
    default = f"/etc/{engine}/config.json"
    custom = f"/opt/custom/{engine}.json"
    router.put(custom, router.path(default).read_text(encoding="utf-8"))
    router.path(default).unlink()
    key = "sing-box.main.conffile" if engine == "sing-box" else "xray.config.conffiles"
    router.set_uci({key: custom})
    router.runtime_paths[engine] = custom
    with loopback_ssh_client(router) as (client, _):
        info = inspect_router(client)
        selected = info.engines[0]
        assert selected.config_path == custom
        assert not selected.issue
        preview = read_router_outbound_summary(
            client, config_path=custom, engine=engine
        )
        assert "router.test" in json.dumps(preview)
        assert f"cat {default}" not in router.calls
        router.path(custom).unlink()
        with pytest.raises(RouterFileReadError) as error:
            read_router_outbound_summary(client, config_path=custom, engine=engine)
        assert error.value.path == custom
        assert client.run("cat /etc/openwrt_release").exit_code == 0


@pytest.mark.parametrize("server_auth", ["none", "empty-password"])
@pytest.mark.parametrize("auth_mode", ["auto", "password"])
def test_real_ssh_accepts_router_without_password(tmp_path, server_auth, auth_mode):
    router = ShellRouter(tmp_path / "router", set())
    with loopback_ssh_client(
        router,
        server_auth=server_auth,
        client_options={"password": None, "auth_mode": auth_mode},
    ) as (client, _):
        assert client.run("cat /etc/openwrt_release").exit_code == 0
        assert router.auth_attempts == (
            ["none"] if server_auth == "none" else ["none", "password"]
        )


@pytest.mark.parametrize(
    "server_auth,options,expected_attempts",
    [
        ("password", {"password": None}, ["none", "password"]),
        ("password", {"password": "wrong-password"}, ["password"]),
        ("none", {"password": "wrong-password"}, ["password"]),
        ("none", {"password": None, "auth_mode": "key"}, []),
    ],
)
def test_real_ssh_rejects_missing_or_wrong_credentials_and_respects_key_mode(
    tmp_path, server_auth, options, expected_attempts
):
    router = ShellRouter(tmp_path / "router", set())
    with loopback_ssh_client(
        router, server_auth=server_auth, client_options=options, connect=False
    ) as (client, _):
        with pytest.raises(SshRouterError):
            client.connect()
        assert client._client is None
        assert router.auth_attempts == expected_attempts


@pytest.mark.parametrize(
    "pin,error_type",
    [(None, UnknownHostKeyError), ("ssh-rsa changed-key", HostKeyMismatchError)],
)
def test_passwordless_auth_does_not_bypass_host_key_confirmation(
    tmp_path, pin, error_type
):
    router = ShellRouter(tmp_path / "router", set())
    with loopback_ssh_client(
        router,
        server_auth="none",
        client_options={"password": None, "trusted_host_key": pin},
        connect=False,
    ) as (client, _):
        with pytest.raises(error_type):
            client.connect()
        assert client._client is None
        assert router.auth_attempts == []
