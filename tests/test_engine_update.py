"""Engine discovery, preview-only migration, and reactive GUI regression tests."""

from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest
from test_auto_outbounds import UUID, xray_server
from test_xray_outbounds import xray_router

from singroute.application.engine_update import _ServiceSwitch, prepare_engine_update
from singroute.application.router_client import CommandResult
from singroute.application.router_connection import (
    inspect_router,
)
from singroute.application.router_update import RouterUpdateError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class PreviewRouter:
    def __init__(self):
        self.commands = []
        self.installed = {"sing-box", "xray"}
        self.active = {"xray"}
        self.stopped_code = 5
        self.settings = {
            "xray.config.conffiles": "/etc/xray/config.json",
            "xray.config.confdir": "/etc/xray",
            "xray.config.format": "json",
            "xray.config.datadir": "/usr/share/xray",
            "xray.enabled.enabled": "1",
        }
        self.files = {
            "/etc/config/xray": "config xray test\n",
            "/etc/xray/config.json": json.dumps(
                {**xray_router(), "outbounds": [xray_server("router-proxy")]}
            ),
            "/etc/sing-box/config.json": json.dumps(
                {
                    "inbounds": [
                        {"type": "tproxy", "listen": "0.0.0.0", "listen_port": 1100}
                    ],
                    "outbounds": [
                        {
                            "type": "vless",
                            "tag": "router-proxy",
                            "server": "old.test",
                            "server_port": 443,
                            "uuid": UUID,
                        }
                    ],
                }
            ),
        }
        self.reads = []

    def run(self, command):
        self.commands.append(command)
        if command == "cat /etc/openwrt_release":
            return CommandResult(command, 0, "OpenWrt test")
        if command.startswith("command -v "):
            name = command.split()[-1]
            return CommandResult(
                command,
                0 if name in self.installed else 1,
                f"/usr/bin/{name}" if name in self.installed else "",
            )
        if command.startswith("uci -q get "):
            text = self.settings.get(command.split()[-1], "")
            return CommandResult(command, 0 if text else 1, text)
        if command.startswith("test -r "):
            return CommandResult(command, 0 if command.split()[-1] in self.files else 1)
        if command.endswith(" status"):
            name = command.split()[0].rsplit("/", 1)[1]
            return CommandResult(
                command,
                0 if name in self.active else self.stopped_code,
                "running\n" if name in self.active else "not running\n",
            )
        if command.endswith(" enabled"):
            return CommandResult(command, 0)
        if command.startswith("test -x /etc/init.d/"):
            return CommandResult(command, 0)
        raise AssertionError("Unexpected mutation during preview")

    def read_text(self, path):
        self.reads.append(path)
        return self.files[path]

    def write_text(self, *_):
        raise AssertionError("Preview must never write")


def test_discovery_prefers_running_xray_and_reads_effective_package_config():
    client = PreviewRouter()
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    assert info.preferred_engine == "xray"
    xray = info.engines[1]
    assert xray.config_path == "/etc/xray/config.json"
    assert xray.running and not xray.issue
    assert xray.xray_asset_directory == "/usr/share/xray"


@pytest.mark.parametrize("installed", [set(), {"xray"}, {"sing-box"}])
def test_discovery_accepts_no_engine_or_either_single_engine(installed):
    client = PreviewRouter()
    client.installed = installed
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    assert {e.name for e in info.available_engines} == installed
    assert (
        info.preferred_engine is None
        if not installed
        else info.preferred_engine in installed
    )


@pytest.mark.parametrize(
    "setting,value",
    [
        ("xray.config.format", "yaml"),
        ("xray.enabled.enabled", "unknown"),
    ],
)
def test_unsupported_package_setup_stays_connected_with_actionable_issue(
    setting, value
):
    client = PreviewRouter()
    client.settings[setting] = value
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    assert info.engines[1].issue
    # Even an unready configuration must not hide an already-running service.
    assert info.engines[1].running


@pytest.mark.parametrize(
    "files,directory",
    [
        ("/etc/xray/config.json", "/etc/xray"),
        ("/etc/xray/vpoint_socks_vmess.json /etc/xray/vpoint_vmess_freedom.json", ""),
        ("", "/etc/xray"),
        ("/etc/xray/other.json", "/etc/unreadable"),
    ],
)
def test_discovery_always_targets_primary_xray_file_without_scanning_others(
    files, directory
):
    client = PreviewRouter()
    client.active = {"sing-box"}
    client.settings["xray.config.conffiles"] = files
    client.settings["xray.config.confdir"] = directory
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    before = deepcopy(client.files)
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    xray = info.engines[1]
    assert xray.config_path == "/etc/xray/config.json" and not xray.issue
    assert xray.xray_needs_single_config
    plan = prepare_engine_update("", client, info, "xray", transfer=True)
    assert plan.primary.preview["old_outbound"] == {"outbounds": []}
    assert "только /etc/xray/config.json" in plan.primary.warnings[0]
    assert client.files == before
    assert not any("vpoint" in path for path in client.reads)


def test_primary_xray_file_missing_is_not_replaced_with_an_example():
    client = PreviewRouter()
    del client.files["/etc/xray/config.json"]
    client.settings["xray.config.conffiles"] = "/etc/xray/vpoint_socks_vmess.json"
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    assert "config.json" in info.engines[1].issue
    assert info.engines[1].config_path == "/etc/xray/config.json"


def test_one_file_with_many_servers_remains_a_supported_router_target():
    from test_xray_outbounds import native_group

    client = PreviewRouter()
    client.files["/etc/xray/config.json"] = json.dumps(
        {
            **xray_router(),
            **{
                k: v
                for k, v in native_group().items()
                if k in {"outbounds", "routing", "burstObservatory"}
            },
        }
    )
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    assert not info.engines[1].issue
    plan = prepare_engine_update(
        json.dumps({"outbounds": [xray_server("new")]}), client, info, "xray"
    )
    assert len(plan.primary.preview["old_outbound"]["outbounds"]) == 12


def test_transfer_reads_existing_source_without_mutation_and_marks_service_switch():
    client = PreviewRouter()
    before = deepcopy(client.files)
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update("", client, info, "sing-box", transfer=True)
    assert plan.switches_engine
    assert plan.source_path == "/etc/xray/config.json"
    assert plan.source_text == before[plan.source_path]
    assert (
        json.loads(plan.primary.updated_config_text)["outbounds"][0]["server"]
        == "server.example.test"
    )
    assert client.files == before


def test_stopped_xray_with_empty_outbounds_is_ready_for_import_and_transfer():
    client = PreviewRouter()
    client.active = {"sing-box"}
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    xray = info.engines[1]
    assert not xray.running and xray.boot_enabled
    assert not xray.issue and not xray.service_state_issue
    assert info.preferred_engine == "sing-box"
    for transfer in (False, True):
        plan = prepare_engine_update(
            "" if transfer else json.dumps({"outbounds": [xray_server("new")]}),
            client,
            info,
            "xray",
            transfer=transfer,
        )
        assert plan.primary.preview["old_outbound"] == {"outbounds": []}
        assert len(json.loads(plan.primary.updated_config_text)["outbounds"]) == 1
        assert (
            json.loads(plan.primary.updated_config_text)["inbounds"]
            == xray_router()["inbounds"]
        )


def test_both_running_is_ambiguous_and_aborts_preview():
    client = PreviewRouter()
    client.active = {"sing-box", "xray"}
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    with pytest.raises(RouterUpdateError, match="Обе службы"):
        prepare_engine_update("", client, info, "sing-box", transfer=True)


def test_failed_secondary_conversion_is_visible_before_any_apply():
    client = PreviewRouter()
    source = {"outbounds": [xray_server("new", "xhttp")]}
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update(
        json.dumps(source), client, info, "xray", update_both=True
    )
    assert plan.primary.has_changes
    assert plan.secondary is None and plan.secondary_error
    assert "только xray" in plan.secondary_error


def test_switch_timeout_reports_old_service_stop_without_launching_target():
    client = PreviewRouter()
    client.active = {"sing-box"}
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    client.commands.clear()

    def stuck_stop(command):
        client.commands.append(command)
        if command == "/etc/init.d/sing-box stop":
            return CommandResult(command, 0)
        assert command.endswith("/etc/init.d/sing-box status")
        return CommandResult(command, 0, "running\n")

    client.run = stuck_stop
    switch = _ServiceSwitch(client, info.engines[1], info.engines)
    assert not switch.activate()
    assert "остановку sing-box" in switch.failure_reason
    assert "xray не запускался" in switch.failure_reason
    assert len(client.commands) == 12
    assert not any("xray" in command for command in client.commands)


@pytest.mark.parametrize(
    "inbounds",
    [
        [],
        [{"type": "tproxy", "listen_port": 1200}],
        [{"type": "redirect", "listen_port": 1100}],
        [{"type": "tun", "interface_name": "singroute-test"}],
    ],
)
@pytest.mark.parametrize("transfer", [False, True])
def test_preparing_transfer_or_import_preserves_inbounds_without_tproxy_requirement(
    inbounds, transfer
):
    client = PreviewRouter()
    client.active = {"sing-box"}
    config = json.loads(client.files["/etc/sing-box/config.json"])
    config["inbounds"] = inbounds
    client.files["/etc/sing-box/config.json"] = json.dumps(config)
    before = deepcopy(client.files)
    info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update(
        "" if transfer else json.dumps({"outbounds": [xray_server("new")]}),
        client,
        info,
        "xray",
        transfer=transfer,
    )
    assert plan.switches_engine and plan.has_changes
    assert plan.source_text == before["/etc/sing-box/config.json"]
    assert (
        json.loads(plan.primary.updated_config_text)["inbounds"]
        == json.loads(before["/etc/xray/config.json"])["inbounds"]
    )
    assert client.files == before


def test_gui_one_control_reacts_to_import_and_refreshes_both_previews(
    tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window._refresh_preview_for_state()
    assert not window.engine_controls.isHidden()
    assert not window.engine_action.isCheckable()
    assert "Перенести" in window.engine_action.text()
    assert "protocol" in window.old_preview.toPlainText()
    assert "/etc/xray/config.json" in window.connection_hint.text()
    source = json.dumps({"outbounds": [xray_server("new")]})
    assert window._import_source_text(source, "Xray", from_file=False)
    assert window.engine_action.isCheckable()
    window.engine_action.click()
    assert set(json.loads(window.new_preview.toPlainText())) == {"xray", "sing-box"}
    window.singbox_radio.setChecked(True)
    assert window.update_plan.primary.engine == "sing-box"
    assert "/etc/sing-box/config.json" in window.connection_hint.text()
    assert "После применения" in window.preview_warning_label.text()
    window._clear_source()
    assert not window.engine_action.isCheckable()
    window.engine_action.click()
    assert (
        window._transfer_requested
        and window.update_plan.source_path == "/etc/xray/config.json"
    )
    assert window.source_editor.toPlainText() == ""
    assert window.apply_button.isEnabled()
    window._set_busy(True)
    assert not window.engine_controls.isEnabled()
    window.deleteLater()
    app.processEvents()


def test_gui_transfer_to_empty_primary_xray_ignores_package_examples(
    tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.active = {"sing-box"}
    config = json.loads(client.files["/etc/sing-box/config.json"])
    config["inbounds"] = [{"type": "redirect", "listen_port": 1100}]
    client.files["/etc/sing-box/config.json"] = json.dumps(config)
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    client.files["/etc/xray/vpoint_socks_vmess.json"] = "package example"
    client.files["/etc/xray/vpoint_vmess_freedom.json"] = "another package example"
    before = deepcopy(client.files)
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window._refresh_preview_for_state()
    assert json.loads(window.old_preview.toPlainText()) == {"outbounds": []}
    assert window.engine_action.isEnabled()
    window.engine_action.click()
    assert window.update_plan.primary.engine == "xray"
    assert window.update_plan.source_path == "/etc/sing-box/config.json"
    assert json.loads(window.new_preview.toPlainText())["protocol"] == "vless"
    assert "только /etc/xray/config.json" in window.preview_warning_label.text()
    assert window.source_warning_label.isHidden()
    assert window.source_warning_label.text() == ""
    assert window.preview_warning_label.parent() is window.new_preview.parent()
    assert window.apply_button.isEnabled()
    assert client.files == before
    assert not any("vpoint" in path for path in client.reads)
    window._invalidate_preview()
    assert window.preview_warning_label.isHidden()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("width", [780, 960])
def test_preview_warnings_do_not_compress_or_overlap_source_controls(
    tmp_path, monkeypatch, width
):
    from pathlib import Path

    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.brand import APP_STYLESHEET
    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    monkeypatch.setattr(MainWindow, "_check_startup_update_when_idle", lambda *_: None)
    app = QApplication.instance() or QApplication([])
    previous_style, previous_font = app.styleSheet(), app.font()
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    window.resize(width, 740)
    window.engine_controls.show()
    window.show_log_check.setChecked(True)
    window.show()
    app.processEvents()
    original_height = window.source_editor.height()
    window._show_preview_warnings(
        (
            "Группа sing-box преобразована в автоматический выбор Xray по задержке.",
            "После применения Xray будет загружать только /etc/xray/config.json. Остальные файлы сохранятся и не будут загружаться.",
            "После применения будет работать Xray; прежняя служба будет остановлена.",
        )
    )
    app.processEvents()
    assert window.source_editor.height() == original_height == 82
    assert (
        window.source_editor.geometry().bottom() < window.paste_button.geometry().top()
    )
    assert window.source_warning_label.isHidden()
    assert window.preview_warning_label.isVisible()
    assert window.old_preview.height() >= 110 and window.new_preview.height() >= 110
    assert (
        window.new_preview.geometry().bottom()
        < window.preview_warning_label.geometry().top()
    )
    window.grab().save(str(tmp_path / "preview-warnings.png"))
    window.hide()
    window.deleteLater()
    app.processEvents()
    app.setStyleSheet(previous_style)
    app.setFont(previous_font)


def test_gui_imports_tiered_xray_group_into_empty_router_template(
    tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore
    from test_xray_outbounds import tiered_group

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.active = {"sing-box"}
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    assert window._import_source_text(
        json.dumps(tiered_group()), "Xray", from_file=False
    )
    assert window.update_plan is not None and window.apply_button.isEnabled()
    assert json.loads(window.old_preview.toPlainText()) == {"outbounds": []}
    assert len(json.loads(window.new_preview.toPlainText())["dependent_balancers"]) == 1
    assert (
        len(json.loads(window.update_plan.primary.updated_config_text)["outbounds"])
        == 11
    )
    window.deleteLater()
    app.processEvents()


def test_failed_transfer_keeps_current_preview_and_disables_stale_plan(
    tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.active = {"sing-box"}
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window._refresh_preview_for_state()
    window.engine_action.click()
    assert window.apply_button.isEnabled()
    old_preview = window.old_preview.toPlainText()
    errors = []

    def failed_worker(action, finished, *_args, **_kwargs):
        try:
            finished(action())
        except ValueError as error:
            errors.append(error)

    # A conversion failure must retain the loaded target preview and must not
    # leave the previously prepared update available for application.
    config = json.loads(client.files["/etc/sing-box/config.json"])
    config["outbounds"][0]["detour"] = "missing-dependency"
    client.files["/etc/sing-box/config.json"] = json.dumps(config)
    monkeypatch.setattr(window, "_run_worker", failed_worker)
    window.engine_action.click()
    assert errors
    assert window.old_preview.toPlainText() == old_preview
    assert window.new_preview.toPlainText() == ""
    assert window.update_plan is None and not window.apply_button.isEnabled()
    assert window._connected_client is client
    window.deleteLater()
    app.processEvents()


def test_gui_transfer_blocked_by_missing_primary_file_has_visible_reason(
    tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.active = {"sing-box"}
    del client.files["/etc/xray/config.json"]
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    workers = []
    monkeypatch.setattr(window, "_run_worker", lambda *a, **kw: workers.append(a))
    window._refresh_preview_for_state()
    assert "config.json" in window.old_preview.toPlainText()
    assert "недоступны" in window.new_preview.toPlainText()
    assert "причина" in window.status_label.text()
    assert not window.engine_action.isEnabled()
    assert "config.json" in window.engine_action.toolTip()
    window.engine_action.click()
    assert not window._transfer_requested and not workers
    # A stale queued handler also explains the refusal instead of doing nothing.
    window._engine_action_clicked()
    assert "Перенос недоступен" in window.new_preview.toPlainText()
    assert not window._transfer_requested and window.update_plan is None
    assert not window.apply_button.isEnabled()

    # Updating the ready target remains available with an imported source,
    # even when the other config is unsupported (independent partial update).
    window._target_engine = "sing-box"
    window.source_editor.setPlainText(json.dumps({"outbounds": [xray_server("new")]}))
    window._sync_engine_controls()
    assert window.engine_action.isEnabled() and window.engine_action.isCheckable()
    window.source_editor.clear()
    window._sync_engine_controls()
    assert not window.engine_action.isEnabled()

    # After correcting the service setup and reconnecting, transfer works.
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = "xray"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window._refresh_preview_for_state()
    assert window.engine_action.isEnabled()
    window.engine_action.click()
    assert window.update_plan.primary.engine == "xray"
    assert window.update_plan.primary.preview["old_outbound"] == {"outbounds": []}
    assert window.apply_button.isEnabled()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("installed", [set(), {"xray"}, {"sing-box"}])
def test_gui_hides_selector_for_zero_or_one_engine_and_explains_missing_install(
    tmp_path, monkeypatch, installed
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.installed = installed
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    window._target_engine = window._router_info.preferred_engine or "sing-box"
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window._refresh_preview_for_state()
    assert window.engine_controls.isHidden()
    if not installed:
        assert "не установлены" in window.old_preview.toPlainText()
        assert not window.apply_button.isEnabled()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("stopped_code", [3, 5, 4, -1])
def test_gui_reads_empty_xray_config_even_when_stopped_or_status_unknown(
    tmp_path, monkeypatch, stopped_code
):
    from PySide6.QtWidgets import QApplication
    from test_gui_worker import FakeCredentialStore

    from singroute.gui.main_window import MainWindow
    from singroute.infrastructure.settings import PortableSettingsStore

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"), FakeCredentialStore()
    )
    client = PreviewRouter()
    client.active = {"sing-box"}
    client.stopped_code = stopped_code
    client.files["/etc/xray/config.json"] = json.dumps(xray_router())
    window._connected_client = client
    window._router_info = inspect_router(
        client, "/etc/sing-box/config.json", "sing-box"
    )
    monkeypatch.setattr(
        window,
        "_run_worker",
        lambda action, finished, *_args, **_kwargs: finished(action()),
    )
    window.xray_radio.setChecked(True)
    assert json.loads(window.old_preview.toPlainText()) == {"outbounds": []}
    assert "Сервер ещё не задан" in window.status_label.text()
    if stopped_code in {3, 5}:
        window.engine_action.click()
        assert window.apply_button.isEnabled()
        assert window.update_plan.switches_engine
    else:
        assert window._router_info.engines[1].service_state_issue
        assert not window.apply_button.isEnabled()
    window.deleteLater()
    app.processEvents()
