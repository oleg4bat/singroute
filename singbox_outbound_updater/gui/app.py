"""GUI application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from singbox_outbound_updater import __version__
from singbox_outbound_updater.gui.main_window import MainWindow


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("sing-box Outbound Updater")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("singbox-outbound-updater")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()
