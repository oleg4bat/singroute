"""GUI application entry point."""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from singroute import __version__
from singroute.application.app_update import (
    retained_update_backup_path,
    signal_update_health,
    take_update_error,
)
from singroute.gui.main_window import MainWindow

INSTANCE_LOCK_FILENAME = ".SingRoute.instance.lock"
ACTIVATION_TIMEOUT_SECONDS = 3


def _application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def _acquire_instance_lock(directory: Path | None = None) -> QLockFile | None:
    lock = QLockFile(
        str((directory or _application_directory()) / INSTANCE_LOCK_FILENAME)
    )
    if not lock.tryLock(0):
        return None
    return lock


def _activation_server_name(directory: Path) -> str:
    normalized = os.path.normcase(str(directory.resolve())).encode("utf-8")
    return f"SingRoute-{hashlib.sha256(normalized).hexdigest()}"


def _activate_window(window: MainWindow) -> None:
    if window.isMinimized():
        if window.windowState() & Qt.WindowState.WindowMaximized:
            window.showMaximized()
        else:
            window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
    if sys.platform == "win32":
        ctypes.windll.user32.SetForegroundWindow(int(window.winId()))


def _start_activation_server(window: MainWindow, directory: Path) -> QLocalServer:
    server = QLocalServer(window)
    name = _activation_server_name(directory)
    QLocalServer.removeServer(name)
    if not server.listen(name):
        raise RuntimeError(
            f"Не удалось запустить канал активации SingRoute: {server.errorString()}"
        )

    def activate_pending() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.disconnectFromServer()
            socket.deleteLater()
            _activate_window(window)

    server.newConnection.connect(activate_pending)
    return server


def _allow_existing_process_to_focus(directory: Path) -> None:
    if sys.platform != "win32":
        return
    lock = QLockFile(str(directory / INSTANCE_LOCK_FILENAME))
    lock_info = lock.getLockInfo()
    if lock_info is not None:
        ctypes.windll.user32.AllowSetForegroundWindow(lock_info[0])


def _request_existing_window(directory: Path) -> bool:
    _allow_existing_process_to_focus(directory)
    name = _activation_server_name(directory)
    deadline = time.monotonic() + ACTIVATION_TIMEOUT_SECONDS
    while True:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(200):
            socket.disconnectFromServer()
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _show_retained_update_backup(window: MainWindow) -> None:
    backup_path = retained_update_backup_path()
    if backup_path is None:
        return
    QMessageBox.warning(
        window,
        "Требуется очистка после обновления",
        "SingRoute запущен, но резервную копию предыдущей версии удалить не "
        "удалось. Следующее автоматическое обновление будет недоступно, пока "
        "этот файл не будет убран.\n\nЗакройте SingRoute, убедитесь, что текущая "
        "версия запускается, затем переместите или удалите файл:\n"
        f"{backup_path}",
    )


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SingRoute")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("SingRoute")
    app.setStyle("Fusion")
    directory = _application_directory()
    instance_lock = _acquire_instance_lock(directory)
    if instance_lock is None:
        if not _request_existing_window(directory):
            QMessageBox.warning(
                None,
                "Не удалось открыть SingRoute",
                "Запущенный экземпляр не отвечает. Попробуйте ещё раз.",
            )
        return 0

    try:
        window = MainWindow()
        try:
            server = _start_activation_server(window, directory)
        except RuntimeError:
            server = None
            QMessageBox.warning(
                window,
                "Повторное открытие недоступно",
                "Повторный запуск может не открыть это окно.",
            )
        window.show()
        update_error = take_update_error()
        if update_error is not None:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.critical(
                    window,
                    "Обновление SingRoute не установлено",
                    "Новая версия не прошла проверку запуска. Updater попытался "
                    f"восстановить и запустить предыдущую версию.\n\n{update_error}",
                ),
            )
        QTimer.singleShot(0, signal_update_health)
        QTimer.singleShot(6000, lambda: _show_retained_update_backup(window))
        try:
            return app.exec()
        finally:
            if server is not None:
                server.close()
    finally:
        instance_lock.unlock()
