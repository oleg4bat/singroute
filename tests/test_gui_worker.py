from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from gui_support import FakeCredentialStore

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QEventLoop,
    QMimeData,
    QPoint,
    QPointF,
    Qt,
    QThread,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

import singroute.gui.app as gui_app_module
import singroute.gui.main_window as main_window_module
from singroute.application.app_update import (
    ReleaseInfo,
    StagedUpdate,
    UpdateCheckResult,
)
from singroute.application.operation import parse_imported_outbound
from singroute.application.router_client import CommandResult, RouterFileReadError
from singroute.application.router_connection import RouterEngine, RouterInfo
from singroute.application.router_update import (
    RouterUpdatePlan,
    RouterUpdateResult,
)
from singroute.gui.advanced_settings import AdvancedSettingsDialog
from singroute.gui.main_window import ConnectedRouter, MainWindow, _ask_yes_no
from singroute.infrastructure.credentials import CredentialTarget
from singroute.infrastructure.settings import AppSettings, PortableSettingsStore
from singroute.infrastructure.ssh_router import (
    HostKeyInfo,
    HostKeyMismatchError,
    SshRouterClient,
    SshRouterError,
    UnknownHostKeyError,
)


def test_auto_import_shows_skipped_nodes_and_replaces_warning_state(
    make_window, tmp_path
):
    from config_samples import xray_group, xray_server

    window = make_window()
    source = xray_group(xray_server("proxy-good"), xray_server("proxy-bad", "xhttp"))
    assert window._import_source_text(json.dumps(source), "Группа", from_file=False)
    assert "№2" in window.source_warning_label.text()
    assert "XHTTP" in window.source_warning_label.text()
    assert not window.source_warning_label.isHidden()
    assert window._import_source_text(
        json.dumps({"outbounds": [{"type": "vless", "server": "one.test"}]}),
        "Сервер",
        from_file=False,
    )
    assert window.source_warning_label.isHidden()
    window._import_source_text(json.dumps(source), "Группа", from_file=False)
    window._clear_source()
    assert window.source_warning_label.text() == ""
    assert window.source_warning_label.isHidden()


def test_auto_import_with_no_compatible_nodes_preserves_previous_source(
    make_window, monkeypatch
):
    from config_samples import xray_group, xray_server

    window = make_window()
    previous = json.dumps({"outbounds": [{"type": "vless", "server": "one.test"}]})
    window._import_source_text(previous, "Сервер", from_file=False)
    messages = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text: messages.append(text)
    )
    source = xray_group(xray_server("proxy-bad", "xhttp"))
    assert not window._import_source_text(json.dumps(source), "Группа", from_file=False)
    assert window.source_editor.toPlainText() == previous
    assert "нет совместимых" in messages[0]


def test_group_preview_renders_masked_members_and_reports_warnings(
    make_window, tmp_path
):
    from config_samples import router_config, xray_group, xray_server

    from singroute.application.operation import prepare_config_update

    window = make_window()
    update = prepare_config_update(
        json.dumps(
            xray_group(xray_server("proxy-good"), xray_server("proxy-bad", "xhttp"))
        ),
        json.dumps(router_config()),
    )
    plan = RouterUpdatePlan(
        "/etc/sing-box/config.json",
        "original",
        json.dumps(update.updated_config),
        update.preview,
        update.has_changes,
        update.warnings,
    )
    window._preview_finished(plan)
    preview = json.loads(window.new_preview.toPlainText())
    assert preview["group"]["tolerance"] == 20
    assert preview["servers"][0]["uuid"] == "***"
    assert window.preview_warning_label.text() == update.warnings[0]
    assert window.apply_button.isEnabled()


@pytest.fixture(autouse=True)
def disable_scheduled_update_check(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        MainWindow, "_check_startup_update_when_idle", lambda _self: None
    )
    yield
    app = QApplication.instance()
    if app is not None:
        app.clipboard().clear()


def test_worker_result_is_delivered_back_to_gui_thread(make_window, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    received: list[object] = []
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)

    def finished(result: object) -> None:
        assert QThread.currentThread() == app.thread()
        received.append(result)
        loop.quit()

    def action():
        assert QThread.currentThread() != app.thread()
        return {"connected": True}

    window._run_worker(
        action,
        finished,
        "Проверка…",
        retry=lambda: None,
        cancellable=True,
    )
    timeout.start(2000)
    loop.exec()

    assert not errors
    assert received == [{"connected": True}]
    assert window._busy is False
    assert window._active_worker is None
    timeout.stop()


def test_connection_worker_uses_gui_thread_snapshot_only(
    make_window,
    monkeypatch: pytest.MonkeyPatch,
):
    window = make_window()
    window.host_edit.setText("router-before-worker")
    window.username_edit.setText("admin")
    window.password_edit.setText("one-time-secret")
    actions = []
    client_arguments: list[dict[str, object]] = []
    inspect_arguments: list[object] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            client_arguments.append(kwargs)

        def connect(self) -> None:
            pass

        def close(self) -> None:
            pass

    def capture_worker(action, *_args, **_kwargs) -> None:
        actions.append(action)

    def fake_inspect(_client, progress):
        inspect_arguments.append(progress)
        return RouterInfo(
            "OpenWrt test",
            (
                RouterEngine(
                    "sing-box",
                    "/usr/bin/sing-box",
                    "/etc/sing-box/config.json",
                    running=True,
                ),
            ),
        )

    monkeypatch.setattr(main_window_module, "SshRouterClient", FakeClient)
    monkeypatch.setattr(main_window_module, "inspect_router", fake_inspect)
    monkeypatch.setattr(window, "_run_worker", capture_worker)

    window._connect_router()

    assert len(actions) == 1
    window.host_edit.setText("router-after-worker-start")
    window.username_edit.setText("changed")
    window.password_edit.setText("changed-secret")

    def fail_if_worker_reads_fields(*_args, **_kwargs):
        raise AssertionError("worker accessed Qt connection fields")

    monkeypatch.setattr(window, "_settings_from_fields", fail_if_worker_reads_fields)
    result = actions[0]()

    assert isinstance(result, ConnectedRouter)
    assert client_arguments == [
        {
            "host": "router-before-worker",
            "user": "admin",
            "port": 22,
            "identity_file": None,
            "password": "one-time-secret",
            "key_passphrase": "one-time-secret",
            "auth_mode": "auto",
            "trusted_host_key": None,
            "cancel_event": window._cancel_event,
            "progress_callback": window.operation_progress.emit,
        }
    ]
    assert inspect_arguments == [window.operation_progress.emit]


def test_noncancellable_operation_uses_its_own_close_message(
    make_window,
    monkeypatch: pytest.MonkeyPatch,
):
    window = make_window()
    captured: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, message: captured.append(message),
    )
    window._busy = True
    window._operation_cancellable = False
    window._busy_close_message = "Дождитесь загрузки обновления SingRoute."
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted() is False
    assert captured == ["Дождитесь загрузки обновления SingRoute."]
    window._busy = False


@pytest.mark.parametrize("frozen", [False, True])
def test_gui_entry_point_configures_and_shows_main_window(
    monkeypatch: pytest.MonkeyPatch,
    frozen: bool,
):
    calls: list[tuple[str, object]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(gui_app_module.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(gui_app_module, "Thread", FakeThread)
    monkeypatch.setattr(
        gui_app_module,
        "notify_executable_changed",
        lambda path: calls.append(("shell_refresh", path)),
    )

    class FakeApplication:
        def __init__(self, arguments: list[str]) -> None:
            calls.append(("arguments", arguments))

        def setApplicationName(self, value: str) -> None:
            calls.append(("name", value))

        def setApplicationVersion(self, value: str) -> None:
            calls.append(("version", value))

        def setOrganizationName(self, value: str) -> None:
            calls.append(("organization", value))

        def setStyle(self, value: str) -> None:
            calls.append(("style", value))

        def setWindowIcon(self, value) -> None:
            calls.append(("icon", value.isNull()))

        def setStyleSheet(self, value: str) -> None:
            calls.append(("stylesheet", "QGroupBox" in value))

        def exec(self) -> int:
            return 7

    class FakeWindow:
        def show(self) -> None:
            calls.append(("window", "shown"))

    class FakeLock:
        def unlock(self) -> None:
            calls.append(("lock", "released"))

    class FakeServer:
        def close(self) -> None:
            calls.append(("server", "closed"))

    monkeypatch.setattr(gui_app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(gui_app_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(
        gui_app_module, "_acquire_instance_lock", lambda _dir: FakeLock()
    )
    monkeypatch.setattr(
        gui_app_module,
        "_start_activation_server",
        lambda _window, _dir: FakeServer(),
    )

    assert gui_app_module.run_gui() == 7
    assert ("name", "SingRoute") in calls
    assert ("organization", "SingRoute") in calls
    assert ("style", "Fusion") in calls
    assert ("icon", False) in calls
    assert ("stylesheet", True) in calls
    assert ("window", "shown") in calls
    assert ("server", "closed") in calls
    assert ("lock", "released") in calls
    assert (("shell_refresh", Path(gui_app_module.sys.executable)) in calls) == (
        frozen and gui_app_module.sys.platform == "win32"
    )


def test_gui_warns_when_previous_executable_could_not_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backup = tmp_path / ".SingRoute.previous.exe"
    messages: list[str] = []
    monkeypatch.setattr(
        gui_app_module,
        "retained_update_backup_path",
        lambda: backup,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: messages.append(message),
    )

    gui_app_module._show_retained_update_backup(object())  # type: ignore[arg-type]

    assert len(messages) == 1
    assert str(backup) in messages[0]
    assert "Следующее автоматическое обновление будет недоступно" in messages[0]


def test_instance_lock_rejects_second_process_in_same_directory(tmp_path: Path):
    first = gui_app_module._acquire_instance_lock(tmp_path)

    assert first is not None
    assert gui_app_module._acquire_instance_lock(tmp_path) is None

    first.unlock()
    replacement = gui_app_module._acquire_instance_lock(tmp_path)
    assert replacement is not None
    replacement.unlock()


def test_advanced_settings_dialog_applies_all_fields(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    settings = AppSettings(
        port=2222,
        auth_mode="key",
        identity_file="old-key",
        check_updates_on_startup=True,
    )
    dialog = AdvancedSettingsDialog(settings)

    dialog.port_spin.setValue(2200)
    dialog.auth_combo.setCurrentIndex(dialog.auth_combo.findData("password"))
    dialog.identity_edit.setText("  new-key  ")
    dialog.check_updates_on_startup.setChecked(False)
    dialog.offer_ruantiblock_start.setChecked(False)
    dialog.apply_to(settings)

    assert settings.port == 2200
    assert settings.auth_mode == "password"
    assert settings.identity_file == "new-key"
    for attribute in (
        "config_path_edit",
        "xray_config_path_edit",
        "service_name_edit",
        "paths_panel",
    ):
        assert not hasattr(dialog, attribute)
    assert settings.check_updates_on_startup is False
    assert settings.offer_ruantiblock_start is False
    dialog.deleteLater()
    app.processEvents()


def test_advanced_settings_key_browser_uses_selected_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = QApplication.instance() or QApplication([])
    selected = tmp_path / "id_ed25519"
    dialog = AdvancedSettingsDialog(AppSettings(identity_file=str(tmp_path)))
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args: (str(selected), "Все файлы (*)"),
    )

    dialog._browse_identity()

    assert dialog.identity_edit.text() == str(selected)
    dialog.deleteLater()
    app.processEvents()


def test_password_eye_toggles_visibility_without_changing_password(
    make_window, tmp_path: Path
):
    window = make_window()
    window.password_edit.setText("router-secret")

    assert window.password_edit.echoMode() == window.password_edit.EchoMode.Password
    assert window.password_visibility_action.isChecked() is False
    assert window.password_visibility_action.toolTip() == "Показать пароль"

    window.password_visibility_action.trigger()

    assert window.password_edit.echoMode() == window.password_edit.EchoMode.Normal
    assert window.password_edit.text() == "router-secret"
    assert window.password_visibility_action.toolTip() == "Скрыть пароль"

    window.password_visibility_action.trigger()

    assert window.password_edit.echoMode() == window.password_edit.EchoMode.Password
    assert window.password_edit.text() == "router-secret"


def test_disabling_password_storage_keeps_entered_password(make_window, tmp_path: Path):
    window = make_window()
    prepared_plan = object()
    window.update_plan = prepared_plan  # type: ignore[assignment]
    window.apply_button.setEnabled(True)
    window.remember_password_check.setChecked(True)
    window.password_edit.setText("router-secret")

    window.remember_password_check.setChecked(False)

    assert window.password_edit.text() == "router-secret"
    assert window.update_plan is prepared_plan
    assert window.apply_button.isEnabled() is True


def test_vault_failure_does_not_persist_remember_password(make_window, tmp_path: Path):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")

    class FailingCredentialStore(FakeCredentialStore):
        def set_password(self, target: object, password: str) -> None:
            raise RuntimeError("vault unavailable")

    window = make_window(settings_store, FailingCredentialStore())
    window.password_edit.setText("router-secret")
    window.remember_password_check.setChecked(True)

    assert window._save_settings() is False

    assert settings_store.load().remember_password is False
    assert window.settings.remember_password is False
    assert window.password_edit.text() == "router-secret"
    assert window.status_label.text() == "Настройки не сохранены"
    assert window.log_edit.isHidden() is False
    assert "vault unavailable" in window.log_edit.toPlainText()


def test_ini_failure_restores_previous_vault_state(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    credentials = FakeCredentialStore()
    window = make_window(settings_store, credentials)
    target = CredentialTarget("192.168.1.1", 22, "root")
    window.password_edit.setText("router-secret")
    window.remember_password_check.setChecked(True)

    def fail_save(settings: AppSettings) -> None:
        raise OSError("read-only directory")

    monkeypatch.setattr(settings_store, "save", fail_save)

    assert window._save_settings() is False

    assert credentials.get_password(target) is None
    assert window.settings.remember_password is False
    assert window.password_edit.text() == "router-secret"
    assert "read-only directory" in window.log_edit.toPlainText()


def test_vault_delete_failure_keeps_remember_password_enabled(
    make_window, tmp_path: Path
):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    settings_store.save(AppSettings(remember_password=True))
    target = CredentialTarget("192.168.1.1", 22, "root")

    class FailingCredentialStore(FakeCredentialStore):
        def delete_password(self, target: object) -> None:
            raise RuntimeError("vault unavailable")

    credentials = FailingCredentialStore()
    credentials.passwords[target] = "saved-secret"
    window = make_window(settings_store, credentials)
    window.password_edit.setText("one-time-secret")
    window.remember_password_check.setChecked(False)

    assert window._save_settings() is False

    assert settings_store.load().remember_password is True
    assert window.settings.remember_password is True
    assert credentials.get_password(target) == "saved-secret"
    assert window.password_edit.text() == "one-time-secret"


def test_changing_router_identity_removes_old_saved_credential(
    make_window, tmp_path: Path
):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    settings_store.save(
        AppSettings(host="old-router", username="root", remember_password=True)
    )
    credentials = FakeCredentialStore()
    window = make_window(settings_store, credentials)
    window.host_edit.setText("new-router")
    window.password_edit.setText("new-password")

    assert window._save_settings() is True

    old_target = CredentialTarget("old-router", 22, "root")
    new_target = CredentialTarget("new-router", 22, "root")
    assert credentials.set_calls == [(new_target, "new-password")]
    assert old_target in credentials.delete_calls
    assert window.password_edit.text() == ""


def test_advanced_update_preference_keeps_prepared_plan(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    window = make_window(settings_store, FakeCredentialStore())
    client = SshRouterClient("192.168.1.1")
    prepared_plan = object()
    window._connected_client = client
    window.update_plan = prepared_plan  # type: ignore[assignment]
    window.apply_button.setEnabled(True)

    class UpdatePreferenceDialog:
        def __init__(self, settings: AppSettings, parent: MainWindow) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def apply_to(self, settings: AppSettings) -> None:
            settings.check_updates_on_startup = False

    monkeypatch.setattr(
        main_window_module,
        "AdvancedSettingsDialog",
        UpdatePreferenceDialog,
    )

    window._open_advanced_settings()

    assert window.settings.check_updates_on_startup is False
    assert window._connected_client is client
    assert window.update_plan is prepared_plan
    assert window.apply_button.isEnabled() is True
    window._connected_client = None
    client.close()


def test_failed_advanced_save_keeps_active_settings_and_plan(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    window = make_window(settings_store, FakeCredentialStore())
    client = SshRouterClient("192.168.1.1")
    prepared_plan = object()
    window._connected_client = client
    window.update_plan = prepared_plan  # type: ignore[assignment]
    window.apply_button.setEnabled(True)

    class PortDialog:
        def __init__(self, settings: AppSettings, parent: MainWindow) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def apply_to(self, settings: AppSettings) -> None:
            settings.port = 2222

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", PortDialog)

    def fail_save(settings: AppSettings) -> None:
        raise OSError("read-only directory")

    monkeypatch.setattr(settings_store, "save", fail_save)

    window._open_advanced_settings()

    assert window.settings.port == 22
    assert window._connected_client is client
    assert window.update_plan is prepared_plan
    assert window.apply_button.isEnabled() is True
    window._connected_client = None
    client.close()


def test_source_is_read_only_and_triggers_preview_immediately(
    make_window, tmp_path: Path
):
    window = make_window()
    calls: list[str] = []
    window._connected_client = SshRouterClient("192.168.1.1")
    window._prepare_preview_if_ready = lambda: calls.append("preview")  # type: ignore[method-assign]

    window._set_source_content('{"outbounds": []}', "Конфиг вставлен из буфера")

    assert window.source_editor.isReadOnly() is True
    assert window.source_editor.toPlainText() == '{"outbounds": []}'
    assert calls == ["preview"]
    assert window.log_edit.isHidden() is True
    window._connected_client = None


@pytest.mark.parametrize("file_error", [True, False])
def test_file_read_failure_keeps_connection_but_transport_failure_disconnects(
    make_window, monkeypatch, file_error
):
    window = make_window()
    client = SshRouterClient("192.168.1.1")
    window._connected_client = window._active_client = client
    window.update_plan = object()
    window.apply_button.setEnabled(True)
    window._show_preview_warnings(("Устаревшее предупреждение",))
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    error = (
        RouterFileReadError("/etc/xray/config.json")
        if file_error
        else SshRouterError("Соединение разорвано")
    )

    window._worker_failed(error)

    assert window._connected_client is (client if file_error else None)
    assert window.update_plan is None
    assert not window.apply_button.isEnabled()
    assert window.preview_warning_label.isHidden()
    assert errors == ([] if file_error else [error])
    window._disconnect_router()


def test_successful_connection_enables_persistent_auto_connect(
    make_window, tmp_path: Path
):
    store = PortableSettingsStore(tmp_path / "settings.ini")
    window = make_window(store, FakeCredentialStore())
    client = SshRouterClient("192.168.1.1")
    window.auto_connect_check.setChecked(True)
    refreshes: list[str] = []
    window._refresh_preview_for_state = lambda: refreshes.append("refresh")  # type: ignore[method-assign]

    window._connection_finished(
        ConnectedRouter(
            client,
            RouterInfo(
                "OpenWrt test",
                (
                    RouterEngine(
                        "sing-box",
                        "/usr/bin/sing-box",
                        "/etc/sing-box/config.json",
                        running=True,
                    ),
                ),
            ),
        )
    )

    assert window._connected_client is client
    assert window.connect_button.text() == "Отключиться"
    assert store.load().auto_connect is True
    assert refreshes == ["refresh"]
    window._connected_client = None


def test_connected_without_source_shows_current_router_outbound(
    make_window, tmp_path: Path
):
    window = make_window()
    current_outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": "current.test",
        "uuid": "***",
    }

    window._current_router_preview_finished(current_outbound)

    assert json.loads(window.old_preview.toPlainText()) == current_outbound
    assert window.new_preview.toPlainText() == ""
    assert window.new_preview.placeholderText() == (
        "Загрузите исходный конфиг для сравнения"
    )
    assert window.apply_button.isEnabled() is False
    assert window.status_label.text() == (
        "Текущий сервер загружен — добавьте исходный конфиг"
    )


def test_ctrl_v_loads_config_but_keeps_normal_line_edit_paste(
    make_window, tmp_path: Path
):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    config_text = '{"outbounds": [{"type": "vless"}]}'
    QApplication.clipboard().setText(config_text)
    window.source_editor.setFocus()
    app.processEvents()

    QTest.keyClick(
        window.source_editor,
        Qt.Key.Key_V,
        Qt.KeyboardModifier.ControlModifier,
    )
    app.processEvents()

    assert window.source_editor.toPlainText() == config_text
    assert window.source_name_label.text() == "Конфиг вставлен из буфера"

    QApplication.clipboard().setText("10.0.0.1")
    window.host_edit.clear()
    window.host_edit.setFocus()
    app.processEvents()
    QTest.keyClick(
        window.host_edit,
        Qt.Key.Key_V,
        Qt.KeyboardModifier.ControlModifier,
    )
    app.processEvents()

    assert window.host_edit.text() == "10.0.0.1"
    assert window.source_editor.toPlainText() == config_text


@pytest.fixture
def clipboard_import_window(make_window):
    return make_window()


def test_paste_nekoray_export_into_xray(make_window, monkeypatch):
    window = make_window()
    window._target_engine = "xray"
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    source = (
        Path(__file__).parent / "fixtures/source_nekoray_vless_reality.json"
    ).read_text(encoding="utf-8")
    QApplication.clipboard().setText(source)

    window.paste_button.click()

    assert warnings == []
    assert window.source_editor.toPlainText() == source
    assert window.source_name_label.text() == "Конфиг вставлен из буфера"
    assert window.source_warning_label.isHidden()


@pytest.mark.parametrize(
    "url_kind,paste_action",
    [
        ("remote", "button"),
        ("local", "button"),
        ("multiple", "button"),
        ("local", "shortcut"),
    ],
)
def test_paste_imports_json_text_even_when_clipboard_also_has_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clipboard_import_window: MainWindow,
    url_kind: str,
    paste_action: str,
):
    app = QApplication.instance() or QApplication([])
    window = clipboard_import_window
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    source = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source_happ_vless_reality_2.json"
        ).read_text(encoding="utf-8")
    )
    stream = source["outbounds"][0]["streamSettings"]
    stream["tcpSettings"] = {}
    stream["realitySettings"].pop("allowInsecure")
    stream["realitySettings"]["fingerprint"] = "firefox"
    config_text = "\ufeff \n" + json.dumps(source, ensure_ascii=False)
    other_path = tmp_path / "other.json"
    other_path.write_text('{"outbounds": [{"type": "trojan"}]}', encoding="utf-8")
    urls = {
        "remote": [QUrl("https://example.test/config")],
        "local": [QUrl.fromLocalFile(str(other_path))],
        "multiple": [
            QUrl.fromLocalFile(str(other_path)),
            QUrl.fromLocalFile(str(tmp_path / "second.json")),
        ],
    }
    mime_data = QMimeData()
    mime_data.setUrls(urls[url_kind])
    mime_data.setText(config_text)
    QApplication.clipboard().setMimeData(mime_data)
    window.show()
    window.source_editor.setFocus()
    app.processEvents()

    if paste_action == "button":
        window.paste_button.click()
    else:
        QTest.keyClick(
            window.source_editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
        )
    app.processEvents()

    assert warnings == []
    assert window.source_editor.toPlainText() == config_text.lstrip("\ufeff")
    assert window.source_name_label.text() == "Конфиг вставлен из буфера"
    assert window.settings.last_import_directory == ""


@pytest.mark.parametrize(
    "mime_kind,paste_action",
    [
        ("text", "button"),
        ("text-and-url", "button"),
        ("url", "button"),
        ("text", "shortcut"),
    ],
)
def test_paste_imports_vless_link_through_real_clipboard(
    monkeypatch: pytest.MonkeyPatch,
    clipboard_import_window: MainWindow,
    mime_kind: str,
    paste_action: str,
):
    app = QApplication.instance() or QApplication([])
    window = clipboard_import_window
    fixture_dir = Path(__file__).parent / "fixtures"
    link = (
        (fixture_dir / "source_happ_vless_ws_tls.txt")
        .read_text(encoding="utf-8")
        .strip()
    )
    source_json = (fixture_dir / "source_happ_vless_ws_tls.json").read_text(
        encoding="utf-8"
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    mime_data = QMimeData()
    if mime_kind in {"text-and-url", "url"}:
        mime_data.setUrls([QUrl(link)])
    if mime_kind in {"text-and-url", "text"}:
        mime_data.setText(link)
    QApplication.clipboard().setMimeData(mime_data)
    window.show()
    window.source_editor.setFocus()
    app.processEvents()

    if paste_action == "button":
        window.paste_button.click()
    else:
        QTest.keyClick(
            window.source_editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
        )
    app.processEvents()

    assert warnings == []
    actual = parse_imported_outbound(window.source_editor.toPlainText())
    expected = parse_imported_outbound(source_json)
    expected.pop("tag")
    assert actual == expected
    assert window.source_name_label.text() == "Конфиг вставлен из буфера"
    assert window.settings.last_import_directory == ""


def test_invalid_vless_link_keeps_previous_import_and_does_not_leak_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clipboard_import_window: MainWindow,
):
    window = clipboard_import_window
    old_source = (
        '{"outbounds": [{"type": "direct", "tag": "previous"}, {"type": "vless"}]}'
    )
    window._set_source_content(old_source, "Предыдущий конфиг")
    other_file = tmp_path / "other.json"
    other_file.write_text('{"outbounds": [{"type": "trojan"}]}', encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(other_file))])
    mime_data.setText("vless://private-uuid@private-server:443?type=ws&security=tls")
    QApplication.clipboard().setMimeData(mime_data)

    window.paste_button.click()

    assert window.source_editor.toPlainText() == old_source
    assert window.source_name_label.text() == "Предыдущий конфиг"
    assert len(warnings) == 1
    assert "UUID" in warnings[0]
    assert "private" not in warnings[0]


def test_open_file_imports_happ_ws_json_and_vless_text(
    monkeypatch: pytest.MonkeyPatch,
    clipboard_import_window: MainWindow,
):
    window = clipboard_import_window
    fixture_dir = Path(__file__).parent / "fixtures"
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    for name in ("source_happ_vless_ws_tls.json", "source_happ_vless_ws_tls.txt"):
        path = fixture_dir / name
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName", lambda *_args, path=path: (str(path), "")
        )
        window.open_file_button.click()
        assert (
            window.source_editor.toPlainText().strip()
            == path.read_text(encoding="utf-8").strip()
        )
        assert window.settings.last_import_directory == str(path.parent)
    assert warnings == []


def test_paste_rejects_invalid_json_text_instead_of_loading_clipboard_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clipboard_import_window: MainWindow,
):
    window = clipboard_import_window
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    existing_text = '{"outbounds": [{"type": "vless"}]}'
    window._set_source_content(existing_text, "Предыдущий конфиг")
    other_path = tmp_path / "other.json"
    other_path.write_text('{"outbounds": [{"type": "trojan"}]}', encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(other_path))])
    mime_data.setText('{"outbounds": [')
    QApplication.clipboard().setMimeData(mime_data)

    window.paste_button.click()

    assert window.source_editor.toPlainText() == existing_text
    assert window.source_name_label.text() == "Предыдущий конфиг"
    assert window.settings.last_import_directory == ""
    assert len(warnings) == 1
    assert "некорректный JSON" in warnings[0]
    assert "Строка 1" in warnings[0]


@pytest.mark.parametrize("import_action", ["button", "shortcut", "file"])
def test_imports_repeated_json_with_visible_notice(
    tmp_path, monkeypatch, clipboard_import_window, import_action
):
    window = clipboard_import_window
    messages = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text: messages.append(text)
    )
    source = Path(__file__).parent / "fixtures" / "source_happ_vless_ws_tls.json"
    text = source.read_text(encoding="utf-8")
    repeated = text.strip() + " " + text.strip()

    if import_action == "file":
        path = tmp_path / "repeated.json"
        path.write_text(repeated, encoding="utf-8")
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
        window.open_file_button.click()
    else:
        QApplication.clipboard().setText(repeated)
        if import_action == "button":
            window.paste_button.click()
        else:
            window.show()
            window.source_editor.setFocus()
            QApplication.instance().processEvents()
            QTest.keyClick(
                window.source_editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
            )

    assert messages == []
    assert window.source_editor.toPlainText() == repeated
    assert parse_imported_outbound(window.source_editor.toPlainText()) == (
        parse_imported_outbound(text)
    )
    assert "Импортирована одна копия" in window.source_warning_label.text()
    assert not window.source_warning_label.isHidden()

    assert window._import_source_text(text, "Один конфиг", from_file=False)
    assert window.source_warning_label.isHidden()


def test_paste_of_differing_json_copies_preserves_previous_source(
    monkeypatch, clipboard_import_window
):
    window = clipboard_import_window
    messages = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text: messages.append(text)
    )
    source = '{"outbounds": [{"type": "vless", "server": "vpn.test"}]}'
    assert window._import_source_text(source, "Предыдущий конфиг", from_file=False)
    QApplication.clipboard().setText(source + source.replace("vpn.test", "other.test"))

    window.paste_button.click()

    assert window.source_editor.toPlainText() == source
    assert window.source_name_label.text() == "Предыдущий конфиг"
    assert len(messages) == 1
    assert "Скопируйте конфиг целиком ещё раз" in messages[0]
    assert "vpn.test" not in messages[0]
    assert "other.test" not in messages[0]


def test_ctrl_v_imports_copied_file_even_when_connection_field_has_focus(
    make_window,
    tmp_path: Path,
):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    config_path = tmp_path / "source.json"
    config_text = '{"outbounds": [{"type": "vless"}]}'
    config_path.write_text(config_text, encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(config_path))])
    QApplication.clipboard().setMimeData(mime_data)
    window.host_edit.setText("router.test")
    window.host_edit.setFocus()
    app.processEvents()

    QTest.keyClick(window.host_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()

    assert window.host_edit.text() == "router.test"
    assert window.source_editor.toPlainText() == config_text
    assert window.source_name_label.text() == "Загружен файл из буфера: source.json"
    assert window.settings.last_import_directory == str(tmp_path)


def test_ctrl_v_reports_unsupported_clipboard_format(
    make_window, monkeypatch: pytest.MonkeyPatch
):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    mime_data = QMimeData()
    mime_data.setData("application/octet-stream", b"not a config")
    QApplication.clipboard().setMimeData(mime_data)
    window.source_editor.setFocus()
    app.processEvents()

    QTest.keyClick(
        window.source_editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
    )
    app.processEvents()

    assert window.source_editor.toPlainText() == ""
    assert warnings == [
        "Данный формат не поддерживается. Вставьте JSON, ссылку vless:// или "
        "скопируйте один файл с конфигурацией."
    ]


def test_copied_file_must_contain_supported_config(
    make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    window = make_window()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    config_path = tmp_path / "unrelated.json"
    config_path.write_text('{"unrelated": true}', encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(config_path))])
    QApplication.clipboard().setMimeData(mime_data)

    window._paste_source()

    assert window.source_editor.toPlainText() == ""
    assert window.settings.last_import_directory == ""
    assert warnings and "Данный формат не поддерживается" in warnings[0]


def test_open_file_uses_same_source_validation(
    make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    window = make_window()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    invalid_path = tmp_path / "unrelated.json"
    invalid_path.write_text('{"unrelated": true}', encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args: (str(invalid_path), "JSON (*.json)"),
    )

    window._load_source_file()

    assert window.source_editor.toPlainText() == ""
    assert window.settings.last_import_directory == ""
    assert warnings and "Данный формат не поддерживается" in warnings[0]


@pytest.mark.parametrize(
    ("network", "expected_message"),
    [
        (
            "http",
            "SingRoute пока не импортирует VLESS с транспортом HTTP.",
        ),
        (
            "xhttp",
            "sing-box на роутере не поддерживает XHTTP. "
            "Выберите у провайдера профиль VLESS Reality через TCP или gRPC.",
        ),
        (
            "private-network-value",
            "SingRoute пока не импортирует VLESS с этим транспортом.",
        ),
    ],
)
def test_open_file_explains_unsupported_vless_transport_without_leaking_values(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: str,
    expected_message: str,
):
    window = make_window()
    window._router_info = RouterInfo(
        "OpenWrt test",
        (
            RouterEngine(
                "sing-box",
                "/usr/bin/sing-box",
                "/etc/sing-box/config.json",
                running=True,
            ),
        ),
    )
    source_config = json.loads(
        (Path(__file__).parent / "fixtures/source_happ_vless_reality_1.json").read_text(
            encoding="utf-8"
        )
    )
    stream_settings = source_config["outbounds"][0]["streamSettings"]
    stream_settings["network"] = network
    stream_settings["grpcSettings"] = {
        "authority": "private-authority.example",
        "multiMode": False,
        "serviceName": "private-service",
    }
    source_path = tmp_path / "unsupported.json"
    source_path.write_text(json.dumps(source_config), encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args: (str(source_path), "JSON (*.json)"),
    )

    window._load_source_file()

    assert warnings == [expected_message]
    assert window.source_editor.toPlainText() == ""
    assert window.settings.last_import_directory == ""


def test_import_accepts_happ_grpc_multi_mode_for_reality(
    make_window, monkeypatch: pytest.MonkeyPatch
):
    window = make_window()
    source = (
        Path(__file__).parent / "fixtures/source_happ_vless_reality_grpc.json"
    ).read_text(encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    imported = window._import_source_text(source, "gRPC", from_file=False)

    assert imported
    assert warnings == []
    assert window.source_editor.toPlainText() == source


def test_import_rejects_custom_grpc_multi_mode_path_without_leaking_service(
    make_window, monkeypatch: pytest.MonkeyPatch
):
    window = make_window()
    window._router_info = RouterInfo(
        "OpenWrt test",
        (
            RouterEngine(
                "sing-box",
                "/usr/bin/sing-box",
                "/etc/sing-box/config.json",
                running=True,
            ),
        ),
    )
    source = json.loads(
        (
            Path(__file__).parent / "fixtures/source_happ_vless_reality_grpc.json"
        ).read_text(encoding="utf-8")
    )
    source["outbounds"][0]["streamSettings"]["grpcSettings"]["serviceName"] = (
        "/private-service/TunMulti"
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    imported = window._import_source_text(json.dumps(source), "gRPC", from_file=False)

    assert not imported
    assert warnings == [
        "Этот gRPC-конфиг использует multiMode с нестандартным "
        "путём или без Reality. Импорт остановлен."
    ]
    assert "private-service" not in warnings[0]


@pytest.mark.parametrize(
    "target_name", ["window", "host_edit", "source_editor", "old_preview"]
)
def test_drop_imports_file_anywhere_in_main_window(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    app.processEvents()
    target = window if target_name == "window" else getattr(window, target_name)
    if target_name in ("source_editor", "old_preview"):
        target = target.viewport()
    config_path = tmp_path / "source.json"
    config_text = '{"outbounds": [{"type": "vless"}]}'
    config_path.write_text(config_text, encoding="utf-8-sig")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(config_path))])
    preview_calls: list[str] = []
    monkeypatch.setattr(
        window, "_prepare_preview_if_ready", lambda: preview_calls.append("prepared")
    )
    offered_actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    enter = QDragEnterEvent(
        QPoint(5, 5),
        offered_actions,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    enter.setDropAction(Qt.DropAction.MoveAction)
    app.sendEvent(target, enter)
    assert window.drop_hint.isVisible()
    assert window.drop_hint.geometry() == window.centralWidget().rect()
    move = QDragMoveEvent(
        QPoint(5, 5),
        offered_actions,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    move.setDropAction(Qt.DropAction.MoveAction)
    app.sendEvent(target, move)
    drop = QDropEvent(
        QPointF(5, 5),
        offered_actions,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    drop.setDropAction(Qt.DropAction.MoveAction)
    app.sendEvent(target, drop)

    assert enter.isAccepted()
    assert move.isAccepted()
    assert drop.isAccepted()
    assert enter.dropAction() == Qt.DropAction.CopyAction
    assert move.dropAction() == Qt.DropAction.CopyAction
    assert drop.dropAction() == Qt.DropAction.CopyAction
    assert config_path.is_file()
    assert not window.drop_hint.isVisible()
    assert window.source_editor.toPlainText() == config_text
    assert window.source_name_label.text() == "Загружен файл: source.json"
    assert window.settings.last_import_directory == str(tmp_path)
    assert preview_calls == ["prepared"]


def test_drag_leave_hides_drop_hint(make_window, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    app.processEvents()
    config_path = tmp_path / "source.json"
    config_path.write_text('{"outbounds": [{"type": "vless"}]}', encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(config_path))])
    enter = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.host_edit, enter)
    assert window.drop_hint.isVisible()

    app.sendEvent(window.host_edit, QDragLeaveEvent())

    assert not window.drop_hint.isVisible()


@pytest.mark.parametrize("source", ["directory", "remote_url", "multiple_files"])
def test_drop_rejects_non_single_local_files(make_window, tmp_path: Path, source: str):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    config_path = tmp_path / "source.json"
    config_path.write_text('{"outbounds": [{"type": "vless"}]}', encoding="utf-8")
    mime_data = QMimeData()
    urls = {
        "directory": [QUrl.fromLocalFile(str(tmp_path))],
        "remote_url": [QUrl("https://example.test/source.json")],
        "multiple_files": [
            QUrl.fromLocalFile(str(config_path)),
            QUrl.fromLocalFile(str(config_path)),
        ],
    }
    mime_data.setUrls(urls[source])

    enter = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.host_edit, enter)
    drop = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.host_edit, drop)

    assert not enter.isAccepted()
    assert not drop.isAccepted()
    assert window.source_editor.toPlainText() == ""
    assert window.settings.last_import_directory == ""


def test_drop_rejected_during_operation(make_window, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    app.processEvents()
    config_path = tmp_path / "source.json"
    config_path.write_text('{"outbounds": [{"type": "vless"}]}', encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(config_path))])
    hover = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.old_preview.viewport(), hover)
    assert hover.isAccepted()
    assert window.drop_hint.isVisible()
    window._set_busy(True)
    assert not window.drop_hint.isVisible()

    enter = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.old_preview.viewport(), enter)
    drop = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window.old_preview.viewport(), drop)

    assert not enter.isAccepted()
    assert not drop.isAccepted()
    assert window.source_editor.toPlainText() == ""
    window._set_busy(False)


def test_drop_uses_file_validation_and_keeps_previous_source_on_error(
    make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = QApplication.instance() or QApplication([])
    window = make_window()
    window.show()
    app.processEvents()
    existing_text = '{"outbounds": [{"type": "vless"}]}'
    window._set_source_content(existing_text, "Предыдущий конфиг")
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text('{"outbounds": [', encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(invalid_path))])

    enter = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window, enter)

    drop = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(window, drop)

    assert window.source_editor.toPlainText() == existing_text
    assert window.source_name_label.text() == "Предыдущий конфиг"
    assert window.settings.last_import_directory == ""
    assert len(warnings) == 1
    assert "некорректный JSON" in warnings[0]


def test_paste_rejects_multiple_copied_files(
    make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    window = make_window()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    mime_data = QMimeData()
    mime_data.setUrls(
        [
            QUrl.fromLocalFile(str(tmp_path / "first.json")),
            QUrl.fromLocalFile(str(tmp_path / "second.json")),
        ]
    )
    QApplication.clipboard().setMimeData(mime_data)

    window._paste_source()

    assert window.source_editor.toPlainText() == ""
    assert warnings and "Данный формат не поддерживается" in warnings[0]


def test_failed_validation_displays_both_engine_streams_in_log(
    make_window, monkeypatch
):
    window = make_window()
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    result = RouterUpdateResult(
        success=False,
        backup_path=None,
        validation_result=CommandResult(
            "xray run -test",
            23,
            stdout="Xray 26.10.21",
            stderr="unsupported config file extension: /tmp/config.new; id=***",
        ),
        message="Конфиг не прошёл проверку.",
    )
    window._update_finished(result)
    log = window.log_edit.toPlainText()
    for text in (
        "код 23",
        "Xray 26.10.21",
        "unsupported config file extension",
        "id=***",
    ):
        assert text in log
        assert text in messages[0]
    assert "вывод скрыт" not in log
    assert window.status_label.text() == result.message
    assert window.show_log_check.isChecked()


def test_rechecked_equal_preview_shows_current_data_without_enabling_update(
    make_window,
):
    window = make_window()
    current_outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": "new.test",
    }
    plan = RouterUpdatePlan(
        config_path="/etc/sing-box/config.json",
        original_config_text="{}",
        updated_config_text="{}",
        preview={
            "old_outbound": current_outbound,
            "new_outbound": dict(current_outbound),
        },
        has_changes=False,
    )

    window._preview_finished(plan)

    assert json.loads(window.old_preview.toPlainText()) == current_outbound
    assert json.loads(window.new_preview.toPlainText()) == current_outbound
    assert window.update_plan is None
    assert window.apply_button.isEnabled() is False
    assert window.status_label.text() == "Конфиг роутера уже соответствует исходному"


def test_yes_no_question_uses_russian_button_labels(make_window, monkeypatch):
    window = make_window()
    captured: dict[str, str] = {}

    def fake_exec(dialog: QMessageBox) -> int:
        yes_button = dialog.button(QMessageBox.StandardButton.Yes)
        no_button = dialog.button(QMessageBox.StandardButton.No)
        captured["yes"] = yes_button.text()
        captured["no"] = no_button.text()
        return QMessageBox.StandardButton.Yes.value

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    answer = _ask_yes_no(
        window,
        "Вопрос",
        "Продолжить?",
        QMessageBox.StandardButton.No,
    )

    assert answer == QMessageBox.StandardButton.Yes
    assert captured == {"yes": "Да", "no": "Нет"}


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    ("answer", "confirmed"),
    [
        (QMessageBox.StandardButton.No, False),
        (QMessageBox.StandardButton.Yes, True),
    ],
)
def test_host_key_requires_confirmation_before_saving_and_retrying(
    make_window,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
    answer: QMessageBox.StandardButton,
    confirmed: bool,
):
    store = PortableSettingsStore(tmp_path / "settings.ini")
    old_pin = "ssh-ed25519 OLD"
    store.save(
        AppSettings(trusted_host_keys={"192.168.1.1:22": old_pin} if changed else {})
    )
    window = make_window(store, FakeCredentialStore())
    info = HostKeyInfo("192.168.1.1", 22, "ssh-ed25519", "NEW", "SHA256:new")
    error = HostKeyMismatchError(info) if changed else UnknownHostKeyError(info)
    retries: list[bool] = []
    prompts: list[tuple[str, str, QMessageBox.StandardButton]] = []

    def ask_yes_no(_parent, title, message, default):
        prompts.append((title, message, default))
        return answer

    monkeypatch.setattr(main_window_module, "_ask_yes_no", ask_yes_no)
    window._retry_action = lambda: retries.append(True)

    window._worker_failed(error)

    assert len(prompts) == 1
    title, message, default = prompts[0]
    assert ("изменился" in title) is changed
    assert "SHA256:new" in message
    assert "на самом роутере" in message
    assert default == QMessageBox.StandardButton.No
    assert store.load().trusted_host_keys.get("192.168.1.1:22") == (
        info.trust_token if confirmed else old_pin if changed else None
    )
    assert retries == ([True] if confirmed else [])


def test_background_update_check_marks_available_version_without_dialog(
    make_window,
):
    window = make_window()
    release = ReleaseInfo(
        version="0.4.0",
        tag="v0.4.0",
        page_url="https://github.com/oleg4bat/singroute/releases/tag/v0.4.0",
        executable_url=(
            "https://github.com/oleg4bat/singroute/releases/download/"
            "v0.4.0/SingRoute.exe"
        ),
        executable_digest="a" * 64,
    )

    window._app_update_check_finished(
        UpdateCheckResult("0.3.0", release, update_available=True),
        silent=True,
    )

    assert window.app_update_button.text() == "Установить v0.4.0"
    assert "Доступно обновление SingRoute v0.4.0" in window.log_edit.toPlainText()


@pytest.mark.parametrize(
    ("answer", "should_install"),
    [
        (QMessageBox.StandardButton.No, False),
        (QMessageBox.StandardButton.Yes, True),
    ],
)
def test_update_install_requires_explicit_confirmation(
    make_window,
    monkeypatch,
    answer: QMessageBox.StandardButton,
    should_install: bool,
):
    window = make_window()
    release = ReleaseInfo(
        version="0.4.0",
        tag="v0.4.0",
        page_url="https://github.com/oleg4bat/singroute/releases/tag/v0.4.0",
        executable_url=(
            "https://github.com/oleg4bat/singroute/releases/download/"
            "v0.4.0/SingRoute.exe"
        ),
        executable_digest="a" * 64,
    )
    downloads: list[ReleaseInfo] = []
    prompts: list[str] = []
    monkeypatch.setattr(
        main_window_module,
        "is_portable_windows_build",
        lambda: True,
    )
    window._download_app_update = downloads.append  # type: ignore[method-assign]

    def ask_yes_no(*args):
        prompts.append(args[2])
        return answer

    monkeypatch.setattr(main_window_module, "_ask_yes_no", ask_yes_no)

    window._offer_app_update(release)

    assert downloads == ([release] if should_install else [])
    assert len(prompts) == 1
    assert "SHA" not in prompts[0]
    assert "контрольн" not in prompts[0]
    assert "автоматически" in prompts[0]


def test_verified_update_launches_helper_and_quits_application(
    make_window,
    tmp_path: Path,
    monkeypatch,
):
    window = make_window()
    target = tmp_path / "SingRoute.exe"
    staged_path = tmp_path / ".SingRoute.update-v0.4.0.exe"
    release = ReleaseInfo(
        version="0.4.0",
        tag="v0.4.0",
        page_url="https://github.com/oleg4bat/singroute/releases/tag/v0.4.0",
        executable_url="https://example.test/SingRoute.exe",
        executable_digest="a" * 64,
    )
    staged = StagedUpdate(release, staged_path, target, "a" * 64)
    launched: list[StagedUpdate] = []
    quit_calls: list[bool] = []

    class FakeApplication:
        def quit(self) -> None:
            quit_calls.append(True)

    class FakeQApplication:
        @staticmethod
        def instance() -> FakeApplication:
            return FakeApplication()

    monkeypatch.setattr(
        main_window_module,
        "launch_staged_update",
        launched.append,
    )
    monkeypatch.setattr(main_window_module, "QApplication", FakeQApplication)

    window._app_update_downloaded(staged)

    assert launched == [staged]
    assert quit_calls == [True]
