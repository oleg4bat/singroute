"""The supported GUI Apply path through real SSH, files and engine processes."""

import json
from pathlib import Path

import pytest
from config_samples import UUID
from gui_support import wait_until
from local_proxy import probe_target, proxy_request
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox
from ssh_test_server import loopback_ssh_client
from xray_test_router import LocalRouter, setup_local

from singroute.application.operation import summarize_router_outbound
from singroute.application.router_connection import inspect_router
from singroute.gui.main_window import ConnectedRouter


@pytest.mark.parametrize("unsupported", [False, True])
def test_engine_switch_keeps_current_server_when_source_conversion_fails(
    tmp_path, monkeypatch, make_window, qt_app, unsupported
):
    router = LocalRouter(tmp_path / "router")
    window = make_window()
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *_: errors.append(_[-1]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: errors.append(_[-1]))
    try:
        setup_local(router)
        assert router.start("xray")
        before = {
            engine: router.read_text(f"/etc/{engine}/config.json")
            for engine in ("sing-box", "xray")
        }
        with loopback_ssh_client(router) as (client, _):
            window._connection_finished(ConnectedRouter(client, inspect_router(client)))
            wait_until(lambda: not window._busy)
            window.singbox_radio.click()
            wait_until(lambda: not window._busy)
            source = json.loads(
                (
                    Path(__file__).parent / "fixtures/source_nekoray_vless_reality.json"
                ).read_text(encoding="utf-8")
            )
            if unsupported:
                source["outbounds"][0]["detour"] = "direct"
            text = json.dumps(source)
            qt_app.clipboard().setText(text)
            window.paste_button.click()
            wait_until(lambda: not window._busy)
            assert window.update_plan is not None
            assert window.apply_button.isEnabled()
            assert not errors

            window.xray_radio.click()
            wait_until(lambda: not window._busy)

            assert json.loads(
                window.old_preview.toPlainText()
            ) == summarize_router_outbound(before["xray"], "xray")
            assert window._connected_client is client
            assert window.source_editor.toPlainText() == text
            assert window._router_info.preferred_engine == "xray"
            if unsupported:
                assert len(errors) == 1 and "безопасного соответствия" in errors[0]
                assert (
                    "Не удалось подготовить изменения"
                    in window.new_preview.toPlainText()
                )
                assert window.update_plan is None
                assert not window.apply_button.isEnabled()
            else:
                assert not errors
                assert window.update_plan.primary.engine == "xray"
                assert window.apply_button.isEnabled()

            window.singbox_radio.click()
            wait_until(lambda: not window._busy)
            assert window.update_plan.primary.engine == "sing-box"
            assert window.apply_button.isEnabled()
            assert json.loads(
                window.old_preview.toPlainText()
            ) == summarize_router_outbound(before["sing-box"])
            assert json.loads(window.new_preview.toPlainText())["type"] == "vless"
            assert UUID not in window.log_edit.toPlainText()
            assert all(
                router.read_text(f"/etc/{engine}/config.json") == original
                for engine, original in before.items()
            )
            assert router.running("xray") and not router.running("sing-box")
            window._disconnect_router()
    finally:
        window._disconnect_router()
        router.close()


@pytest.mark.parametrize(
    "scenario", ["update", "retry", "unknown", "switch-to-sing-box"]
)
def test_apply_from_gui_with_real_ssh_and_engines(
    tmp_path, monkeypatch, make_window, qt_app, scenario
):
    router = LocalRouter(tmp_path / "router")
    window = make_window()
    window.settings.offer_ruantiblock_start = False
    engine = "sing-box" if scenario == "switch-to-sing-box" else "xray"
    custom = f"/opt/custom/{engine}.json"
    default = f"/etc/{engine}/config.json"
    key = "sing-box.main.conffile" if engine == "sing-box" else "xray.config.conffiles"
    results, errors, warnings, information = [], [], [], []
    original_finished = window._update_finished
    answer = QMessageBox.StandardButton.Yes

    def finished(result, plan):
        assert QThread.currentThread() == qt_app.thread()
        results.append(result)
        original_finished(result, plan)

    def confirm(dialog):
        QTimer.singleShot(0, dialog.button(answer).click)
        return original_exec(dialog)

    original_exec = QMessageBox.exec
    monkeypatch.setattr(QMessageBox, "exec", confirm)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args: information.append(args[-1])
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(window, "_update_finished", finished)
    try:
        source, xray_proxy, singbox_proxy = setup_local(router)
        original = router.read_text(default)
        router.put(custom, original)
        router.set_uci_option(f"/etc/config/{engine}", key, custom)
        service_settings = router.read_text(f"/etc/config/{engine}")
        assert router.start("xray")
        other = "/opt/custom/concurrent.json"
        real_run = router.run
        changed = False

        def run(command, input_text=None):
            nonlocal changed
            if (
                scenario == "unknown"
                and command == "/etc/init.d/xray restart"
                and not changed
            ):
                changed = True
                router.put(other, original)
                router.set_uci_option("/etc/config/xray", key, other)
            return real_run(command, input_text)

        monkeypatch.setattr(router, "run", run)
        with loopback_ssh_client(router, connect=False) as (connection, _):
            # Use the product's client creation and pinned-host-key handling.
            window.settings.port = connection.port
            window.settings.auth_mode = "password"
            window.settings.trusted_host_keys = {
                f"127.0.0.1:{connection.port}": connection.trusted_host_key
            }
            window.host_edit.setText("127.0.0.1")
            window.username_edit.setText("root")
            window.password_edit.setText("test-password")
            window.connect_button.click()
            wait_until(lambda: not window._busy)
            assert not errors
            assert window._connected_client is not None
            source_path = tmp_path / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            monkeypatch.setattr(
                QFileDialog, "getOpenFileName", lambda *_: (str(source_path), "")
            )
            window.open_file_button.click()
            wait_until(lambda: not window._busy)
            if engine == "sing-box":
                window.singbox_radio.click()
                wait_until(lambda: not window._busy)
            assert not errors
            plan = window.update_plan
            assert plan is not None and plan.primary.config_path == custom
            assert window.apply_button.isEnabled()
            assert router.read_text(custom) == original
            if scenario == "update":
                answer = QMessageBox.StandardButton.No
                before_decline = len(router.calls)
                preview = window.new_preview.toPlainText()
                window.apply_button.click()
                wait_until(lambda: not window._busy)
                assert not results and not errors and not warnings and not information
                assert len(router.calls) == before_decline
                assert router.read_text(custom) == original
                assert window.update_plan is plan and window.apply_button.isEnabled()
                assert window.new_preview.toPlainText() == preview
                answer = QMessageBox.StandardButton.Yes
            if scenario == "retry":
                router.fail_restart = "xray"
            before_apply = len(router.calls)
            window.apply_button.click()
            wait_until(lambda: not window._busy)
            assert not errors
            result = results[-1]
            assert router.read_text(default) == original
            if scenario == "unknown":
                assert not result.success and result.rollback_success is False
                assert not result.service_state_known and not result.backup_deleted
                assert router.path(result.backup_path).is_file()
                assert router.read_text(other) == original
                assert (
                    router.uci_values(router.read_text("/etc/config/xray"))[key]
                    == other
                )
                assert window._connected_client is None
                assert not window.apply_button.isEnabled()
                assert "переподключитесь" in window.status_label.text()
                assert not information and warnings
                return
            if scenario == "retry":
                assert not result.success and result.rollback_success is True
                assert result.service_state_known and warnings
                assert router.read_text(custom) == original
                assert router.running("xray")
                assert router.read_text("/etc/config/xray") == service_settings
                assert window._connected_client is not None
                assert window.update_plan is plan and window.apply_button.isEnabled()
                before_apply = len(router.calls)
                window.apply_button.click()
                wait_until(lambda: not window._busy)
                result = results[-1]
            assert result.success and result.backup_deleted
            if engine == "xray":
                assert len(router.calls) - before_apply <= 16
            assert router.read_text(custom) == plan.primary.updated_config_text
            assert router.read_text(f"/etc/config/{engine}") == service_settings
            assert router.running(engine)
            assert not router.running("xray" if engine == "sing-box" else "sing-box")
            assert not router.overlapping_engines
            if engine == "sing-box":
                assert router.boot == {"sing-box": True, "xray": False}
            with probe_target() as target:
                assert proxy_request(
                    singbox_proxy if engine == "sing-box" else xray_proxy, target
                )
            assert window._router_info.preferred_engine == engine
            assert (
                json.loads(window.old_preview.toPlainText())
                == plan.primary.preview["new_outbound"]
            )
            assert not window.new_preview.toPlainText()
            assert window.update_plan is None and not window.apply_button.isEnabled()
            assert UUID not in window.log_edit.toPlainText()
            assert "test-password" not in window.log_edit.toPlainText()
            assert not errors and information
            window._disconnect_router()
    finally:
        window._disconnect_router()
        router.close()
