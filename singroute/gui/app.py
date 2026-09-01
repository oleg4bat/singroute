"""GUI application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from singroute import __version__
from singroute.application.app_update import (
    retained_update_backup_path,
    signal_update_health,
    take_update_error,
)
from singroute.gui.main_window import MainWindow

INSTANCE_LOCK_FILENAME = ".SingRoute.instance.lock"


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
    instance_lock = _acquire_instance_lock()
    if instance_lock is None:
        QMessageBox.information(
            None,
            "SingRoute уже запущен",
            "Другой экземпляр SingRoute уже работает из этой папки.",
        )
        return 0

    try:
        window = MainWindow()
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
        return app.exec()
    finally:
        instance_lock.unlock()
