"""GUI application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from singroute import __version__
from singroute.gui.main_window import MainWindow


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SingRoute")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("SingRoute")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()
