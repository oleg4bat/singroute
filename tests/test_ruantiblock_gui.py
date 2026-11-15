from __future__ import annotations

import pytest
from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import singroute.gui.main_window as main_window_module
from singroute.application.router_client import CommandResult
from singroute.application.router_update import RouterUpdateResult
from singroute.application.ruantiblock import (
    SERVICE_CHECK,
    START_COMMAND,
    STATUS_COMMAND,
    RuantiblockState,
)
from singroute.gui.main_window import MainWindow, _ask_ruantiblock_start
from singroute.infrastructure.ssh_router import SshRouterError


@pytest.fixture
def window(make_window):
    return make_window()


@pytest.fixture
def local_service_ssh():
    from ssh_test_server import loopback_ssh_client

    state = {"present": True, "status": 2, "start_exit": 0, "after_start": 0}

    class Service:
        def __init__(self):
            self.calls = []

        def run(self, command, input_text=None):
            self.calls.append(command)
            if command == SERVICE_CHECK:
                return CommandResult(command, 0 if state["present"] else 1)
            if command == STATUS_COMMAND:
                return CommandResult(command, state["status"], f"{state['status']}\n")
            assert command == START_COMMAND
            state["status"] = state["after_start"]
            return CommandResult(command, state["start_exit"])

    service = Service()
    with loopback_ssh_client(service) as (client, _):
        yield client, state, service.calls


def update_result(success=True):
    return RouterUpdateResult(
        success=success,
        backup_path="/etc/sing-box/config.json.bak-test",
        validation_result=CommandResult("sing-box check", 0),
        backup_deleted=success,
        message="Готово — конфиг роутера обновлён." if success else "Выполнен откат.",
    )


@pytest.mark.parametrize("state", [RuantiblockState.DISABLED, RuantiblockState.UNKNOWN])
def test_only_confirmed_disabled_service_prompts(window, monkeypatch, state):
    calls = []
    monkeypatch.setattr(
        main_window_module,
        "_ask_ruantiblock_start",
        lambda _: (calls.append("ask") or False, False),
    )
    monkeypatch.setattr(window, "_prepare_preview_if_ready", lambda: None)

    window._ruantiblock_checked(state)

    assert calls == (["ask"] if state == RuantiblockState.DISABLED else [])


def test_failed_update_does_not_check_ruantiblock(window, monkeypatch):
    calls = []
    monkeypatch.setattr(
        window, "_check_ruantiblock_after_update", lambda: calls.append(True)
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    window._update_finished(update_result(success=False))
    assert calls == []


@pytest.mark.parametrize("start", [False, True])
def test_suppression_persists_with_either_answer_and_skips_future_checks(
    window, monkeypatch, start
):
    monkeypatch.setattr(
        main_window_module, "_ask_ruantiblock_start", lambda _: (start, True)
    )
    starts = []
    monkeypatch.setattr(window, "_start_ruantiblock", lambda: starts.append(True))

    window._ruantiblock_checked(RuantiblockState.DISABLED)

    assert starts == ([True] if start else [])
    assert window.settings.offer_ruantiblock_start is False
    assert window.settings_store.load().offer_ruantiblock_start is False
    # A fresh application instance also skips the check, without network I/O.
    reopened = MainWindow(window.settings_store)
    reopened._connected_client = object()
    reopened._check_ruantiblock_after_update()
    assert reopened._active_worker is None
    reopened._connected_client = None
    reopened.deleteLater()


def test_failed_suppression_save_is_reported_and_does_not_disable_offer(
    window, monkeypatch
):
    monkeypatch.setattr(
        main_window_module, "_ask_ruantiblock_start", lambda _: (False, True)
    )
    monkeypatch.setattr(window, "_prepare_preview_if_ready", lambda: None)
    monkeypatch.setattr(
        window.settings_store,
        "save",
        lambda _: (_ for _ in ()).throw(OSError("read-only")),
    )
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))

    window._ruantiblock_checked(RuantiblockState.DISABLED)

    assert window.settings.offer_ruantiblock_start is True
    assert window.settings_store.load().offer_ruantiblock_start is True
    assert warnings == ["Не удалось сохранить выбор «Не показывать больше»."]


@pytest.mark.parametrize(
    "answer", [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]
)
def test_dialog_has_russian_labels_safe_default_and_suppression(
    window, monkeypatch, answer
):
    def inspect_dialog(dialog):
        assert dialog.windowTitle() == "Включить ruantiblock?"
        assert "установлен ruantiblock" in dialog.text()
        assert "выключен" in dialog.text()
        assert dialog.button(QMessageBox.StandardButton.Yes).text() == "Включить"
        assert dialog.button(QMessageBox.StandardButton.No).text() == "Не сейчас"
        assert dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.No)
        assert dialog.escapeButton() is dialog.button(QMessageBox.StandardButton.No)
        assert dialog.checkBox().text() == "Не показывать больше"
        assert not dialog.checkBox().isChecked()
        dialog.checkBox().setChecked(True)
        return answer.value

    monkeypatch.setattr(QMessageBox, "exec", inspect_dialog)
    assert _ask_ruantiblock_start(window) == (
        answer == QMessageBox.StandardButton.Yes,
        True,
    )


def test_optional_ssh_failure_keeps_success_and_disconnects(window, monkeypatch):
    closed = []

    class Client:
        def close(self):
            closed.append(True)

    window._connected_client = Client()
    window._active_client = window._connected_client
    window._failure_handler = lambda error: window._ruantiblock_failed(
        error, starting=False
    )
    window._retry_action = lambda: pytest.fail("Must not retry the config update")
    window._set_busy(True)
    warnings = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: warnings.append(args))

    window._worker_failed(SshRouterError("Connection lost"))

    assert closed == [True]
    assert window._connected_client is None
    assert window._busy is False
    assert "Конфиг роутера обновлён" in window.status_label.text()
    assert "Не удалось проверить ruantiblock" in window.status_label.text()
    assert warnings == []


@pytest.mark.parametrize(
    (
        "present",
        "state_code",
        "consent",
        "start_exit",
        "after_start",
        "prompted",
        "started",
        "warned",
    ),
    [
        (True, 2, False, 0, 0, True, False, False),
        (True, 2, True, 0, 0, True, True, False),
        (True, 2, True, 1, 1, True, True, True),
        (True, 2, True, 0, 1, True, True, True),
    ],
)
def test_post_update_path_with_real_ssh_workers_and_modal_dialog(
    window,
    monkeypatch,
    local_service_ssh,
    present,
    state_code,
    consent,
    start_exit,
    after_start,
    prompted,
    started,
    warned,
):
    client, state, commands = local_service_ssh
    state.update(
        present=present,
        status=state_code,
        start_exit=start_exit,
        after_start=after_start,
    )
    window._connected_client = client
    client.progress_callback = window.operation_progress.emit
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    prompts = []
    warnings = []
    information = []
    gui_thread = QApplication.instance().thread()

    original_exec = QMessageBox.exec

    def answer_dialog(dialog):
        assert QThread.currentThread() == gui_thread
        prompts.append(dialog.text())
        button = (
            QMessageBox.StandardButton.Yes if consent else QMessageBox.StandardButton.No
        )
        QTimer.singleShot(0, dialog.button(button).click)
        return original_exec(dialog)

    monkeypatch.setattr(QMessageBox, "exec", answer_dialog)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args: information.append(args[2])
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    for name in ("_ruantiblock_checked", "_ruantiblock_started", "_ruantiblock_failed"):
        original = getattr(window, name)

        def finished(*args, _original=original, **kwargs):
            assert QThread.currentThread() == gui_thread
            _original(*args, **kwargs)
            if not window._busy:
                loop.quit()

        monkeypatch.setattr(window, name, finished)
    # Guard the boundary: both status reads and activation must be off the GUI thread.
    original_run = client.run

    def run(command):
        assert QThread.currentThread() != gui_thread
        return original_run(command)

    monkeypatch.setattr(client, "run", run)
    timeout.start(4000)
    window._update_finished(update_result())
    loop.exec()
    timeout.stop()

    assert set(commands) <= {SERVICE_CHECK, STATUS_COMMAND, START_COMMAND}
    assert bool(prompts) == prompted
    assert (START_COMMAND in commands) == started
    assert bool(warnings) == warned
    assert window._busy is False
    assert window.update_plan is None
    assert (
        "Конфиг роутера обновлён" in window.status_label.text()
        or "конфиг роутера обновлён" in window.status_label.text()
    )
    assert ("ruantiblock включён." in information) == (started and not warned)
    assert all(" enable" not in command for command in commands)
