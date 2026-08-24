"""Small QThreadPool worker used for blocking SSH operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class Worker(QRunnable):
    def __init__(self, action: Callable[[], Any]) -> None:
        super().__init__()
        self.action = action
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.action()
        except Exception as error:
            self.signals.failed.emit(error)
        else:
            self.signals.finished.emit(result)
