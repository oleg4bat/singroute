from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

import singroute.gui.app as gui_app_module
import singroute.gui.main_window as main_window_module
from singroute.application.app_update import (
    ReleaseInfo,
    StagedUpdate,
    UpdateCheckResult,
)
from singroute.application.router_client import CommandResult
from singroute.application.router_connection import RouterInfo
from singroute.application.router_update import (
    RouterUpdatePlan,
    RouterUpdateResult,
)
from singroute.gui.advanced_settings import AdvancedSettingsDialog
from singroute.gui.main_window import ConnectedRouter, MainWindow, _ask_yes_no
from singroute.infrastructure.credentials import CredentialTarget
from singroute.infrastructure.settings import AppSettings, PortableSettingsStore
from singroute.infrastructure.ssh_router import SshRouterClient


def test_worker_result_is_delivered_back_to_gui_thread(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    received: list[object] = []

    def finished(result: object) -> None:
        received.append(result)
        loop.quit()

    window._run_worker(
        lambda: {"connected": True},
        finished,
        "Проверка…",
        retry=lambda: None,
        cancellable=True,
    )
    timeout.start(2000)
    loop.exec()

    assert received == [{"connected": True}]
    assert window._busy is False
    assert window._active_worker is None
    timeout.stop()
    window.deleteLater()
    app.processEvents()


def test_noncancellable_operation_uses_its_own_close_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


class FakeCredentialStore:
    def __init__(self) -> None:
        self.passwords: dict[object, str] = {}
        self.set_calls: list[tuple[object, str]] = []
        self.delete_calls: list[object] = []

    def get_password(self, target: object) -> str | None:
        return self.passwords.get(target)

    def set_password(self, target: object, password: str) -> None:
        self.set_calls.append((target, password))
        self.passwords[target] = password

    def delete_password(self, target: object) -> None:
        self.delete_calls.append(target)
        self.passwords.pop(target, None)


def test_gui_entry_point_configures_and_shows_main_window(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, object]] = []

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

        def exec(self) -> int:
            return 7

    class FakeWindow:
        def show(self) -> None:
            calls.append(("window", "shown"))

    class FakeLock:
        def unlock(self) -> None:
            calls.append(("lock", "released"))

    monkeypatch.setattr(gui_app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(gui_app_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(gui_app_module, "_acquire_instance_lock", FakeLock)

    assert gui_app_module.run_gui() == 7
    assert ("name", "SingRoute") in calls
    assert ("organization", "SingRoute") in calls
    assert ("style", "Fusion") in calls
    assert ("window", "shown") in calls
    assert ("lock", "released") in calls


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
        config_path="/old/config.json",
        service_name="old-service",
        check_updates_on_startup=True,
    )
    dialog = AdvancedSettingsDialog(settings)

    dialog.port_spin.setValue(2200)
    dialog.auth_combo.setCurrentIndex(dialog.auth_combo.findData("password"))
    dialog.identity_edit.setText("  new-key  ")
    dialog.config_path_edit.setText("  /new/config.json  ")
    dialog.service_name_edit.setText("  sing-box-new  ")
    dialog.check_updates_on_startup.setChecked(False)
    dialog.apply_to(settings)

    assert settings.port == 2200
    assert settings.auth_mode == "password"
    assert settings.identity_file == "new-key"
    assert settings.config_path == "/new/config.json"
    assert settings.service_name == "sing-box-new"
    assert settings.check_updates_on_startup is False
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


def test_password_eye_toggles_visibility_without_changing_password(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


def test_disabling_password_storage_keeps_entered_password(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
    prepared_plan = object()
    window.update_plan = prepared_plan  # type: ignore[assignment]
    window.apply_button.setEnabled(True)
    window.remember_password_check.setChecked(True)
    window.password_edit.setText("router-secret")

    window.remember_password_check.setChecked(False)

    assert window.password_edit.text() == "router-secret"
    assert window.update_plan is prepared_plan
    assert window.apply_button.isEnabled() is True
    window.deleteLater()
    app.processEvents()


def test_vault_failure_does_not_persist_remember_password(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")

    class FailingCredentialStore(FakeCredentialStore):
        def set_password(self, target: object, password: str) -> None:
            raise RuntimeError("vault unavailable")

    window = MainWindow(settings_store, FailingCredentialStore())
    window.password_edit.setText("router-secret")
    window.remember_password_check.setChecked(True)

    assert window._save_settings() is False

    assert settings_store.load().remember_password is False
    assert window.settings.remember_password is False
    assert window.password_edit.text() == "router-secret"
    assert window.status_label.text() == "Настройки не сохранены"
    assert window.log_edit.isHidden() is False
    assert "vault unavailable" in window.log_edit.toPlainText()
    window.deleteLater()
    app.processEvents()


def test_ini_failure_restores_previous_vault_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    credentials = FakeCredentialStore()
    window = MainWindow(settings_store, credentials)
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
    window.deleteLater()
    app.processEvents()


def test_vault_delete_failure_keeps_remember_password_enabled(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    settings_store.save(AppSettings(remember_password=True))
    target = CredentialTarget("192.168.1.1", 22, "root")

    class FailingCredentialStore(FakeCredentialStore):
        def delete_password(self, target: object) -> None:
            raise RuntimeError("vault unavailable")

    credentials = FailingCredentialStore()
    credentials.passwords[target] = "saved-secret"
    window = MainWindow(settings_store, credentials)
    window.password_edit.setText("one-time-secret")
    window.remember_password_check.setChecked(False)

    assert window._save_settings() is False

    assert settings_store.load().remember_password is True
    assert window.settings.remember_password is True
    assert credentials.get_password(target) == "saved-secret"
    assert window.password_edit.text() == "one-time-secret"
    window.deleteLater()
    app.processEvents()


def test_changing_router_identity_removes_old_saved_credential(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    settings_store.save(
        AppSettings(host="old-router", username="root", remember_password=True)
    )
    credentials = FakeCredentialStore()
    window = MainWindow(settings_store, credentials)
    window.host_edit.setText("new-router")
    window.password_edit.setText("new-password")

    assert window._save_settings() is True

    old_target = CredentialTarget("old-router", 22, "root")
    new_target = CredentialTarget("new-router", 22, "root")
    assert credentials.set_calls == [(new_target, "new-password")]
    assert old_target in credentials.delete_calls
    assert window.password_edit.text() == ""
    window.deleteLater()
    app.processEvents()


def test_advanced_update_preference_keeps_prepared_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    window = MainWindow(settings_store, FakeCredentialStore())
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
    window.deleteLater()
    app.processEvents()


def test_failed_advanced_save_keeps_active_settings_and_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = QApplication.instance() or QApplication([])
    settings_store = PortableSettingsStore(tmp_path / "settings.ini")
    window = MainWindow(settings_store, FakeCredentialStore())
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
    window.deleteLater()
    app.processEvents()


def test_source_is_read_only_and_triggers_preview_immediately(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
    calls: list[str] = []
    window._connected_client = SshRouterClient("192.168.1.1")
    window._prepare_preview_if_ready = lambda: calls.append("preview")  # type: ignore[method-assign]

    window._set_source_content('{"outbounds": []}', "Конфиг вставлен из буфера")

    assert window.source_editor.isReadOnly() is True
    assert window.source_editor.toPlainText() == '{"outbounds": []}'
    assert calls == ["preview"]
    assert window.log_edit.isHidden() is True
    window._connected_client = None
    window.deleteLater()
    app.processEvents()


def test_successful_connection_enables_persistent_auto_connect(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    store = PortableSettingsStore(tmp_path / "settings.ini")
    window = MainWindow(store, FakeCredentialStore())
    client = SshRouterClient("192.168.1.1")
    window.auto_connect_check.setChecked(True)
    refreshes: list[str] = []
    window._refresh_preview_for_state = lambda: refreshes.append("refresh")  # type: ignore[method-assign]

    window._connection_finished(
        ConnectedRouter(client, RouterInfo("OpenWrt test", "/usr/bin/sing-box"))
    )

    assert window._connected_client is client
    assert window.connect_button.text() == "Отключиться"
    assert store.load().auto_connect is True
    assert refreshes == ["refresh"]
    window._connected_client = None
    window.deleteLater()
    app.processEvents()


def test_connected_without_source_shows_current_router_outbound(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
        "Текущий outbound загружен — добавьте исходный конфиг"
    )
    window.deleteLater()
    app.processEvents()


def test_ctrl_v_loads_config_but_keeps_normal_line_edit_paste(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.close()
    window.deleteLater()
    app.processEvents()


def test_successful_update_clears_stale_preview_and_rechecks_router(
    tmp_path: Path,
    monkeypatch,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
    window.old_preview.setPlainText("old")
    window.new_preview.setPlainText("new")
    window.apply_button.setEnabled(True)
    rechecks: list[str] = []
    window._prepare_preview_if_ready = lambda: rechecks.append("recheck")  # type: ignore[method-assign]
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    result = RouterUpdateResult(
        success=True,
        backup_path="/etc/sing-box/config.json.bak-test",
        validation_result=CommandResult("sing-box check", 0),
        backup_deleted=True,
        message="Готово",
    )

    window._update_finished(result)

    assert window.old_preview.toPlainText() == ""
    assert window.new_preview.toPlainText() == ""
    assert window.apply_button.isEnabled() is False
    assert rechecks == ["recheck"]
    window.deleteLater()
    app.processEvents()


def test_rechecked_equal_preview_shows_current_data_without_enabling_update(
    tmp_path: Path,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


def test_yes_no_question_uses_russian_button_labels(tmp_path: Path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


def test_background_update_check_marks_available_version_without_dialog(
    tmp_path: Path,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize(
    ("answer", "should_install"),
    [
        (QMessageBox.StandardButton.No, False),
        (QMessageBox.StandardButton.Yes, True),
    ],
)
def test_update_install_requires_explicit_confirmation(
    tmp_path: Path,
    monkeypatch,
    answer: QMessageBox.StandardButton,
    should_install: bool,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()


def test_verified_update_launches_helper_and_quits_application(
    tmp_path: Path,
    monkeypatch,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        PortableSettingsStore(tmp_path / "settings.ini"),
        FakeCredentialStore(),
    )
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
    window.deleteLater()
    app.processEvents()
