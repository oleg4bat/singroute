from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton

from singroute.gui.advanced_settings import AdvancedSettingsDialog
from singroute.gui.brand import APP_STYLESHEET, ARROW_DIRECTORY, ICON_PATH, brand_icon
from singroute.gui.main_window import MainWindow
from singroute.infrastructure.settings import AppSettings, PortableSettingsStore


def test_brand_assets_cover_windows_icon_sizes():
    app = QApplication.instance() or QApplication([])
    icon = brand_icon()
    assert ICON_PATH.is_file()
    assert not icon.isNull()
    for size in (16, 32, 48, 256):
        assert not icon.pixmap(size, size).isNull()

    ico = ICON_PATH.with_suffix(".ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", ico)
    assert (reserved, kind, count) == (0, 1, 7)
    sizes = [
        256 if ico[6 + 16 * index] == 0 else ico[6 + 16 * index]
        for index in range(count)
    ]
    assert sizes == [16, 24, 32, 48, 64, 128, 256]
    app.processEvents()


def test_main_window_has_brand_icon(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(PortableSettingsStore(tmp_path / "settings.ini"))
    try:
        assert not window.windowIcon().isNull()
        assert window.source_group.title() == "2. Источник сервера"
        assert window.advanced_button.text() == ""
        assert not window.advanced_button.icon().isNull()
    finally:
        window.deleteLater()
        app.processEvents()


def test_desktop_layout_centers_password_action_and_prioritizes_comparison(
    tmp_path: Path,
):
    app = QApplication.instance() or QApplication([])
    previous_style = app.styleSheet()
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow(PortableSettingsStore(tmp_path / "settings.ini"))
    try:
        window.resize(960, 720)
        window.show()
        app.processEvents()

        eye = next(
            button
            for button in window.password_edit.findChildren(QToolButton)
            if button.defaultAction() is window.password_visibility_action
        )
        assert eye.isVisible()
        assert eye.y() >= 0
        assert eye.y() + eye.height() <= window.password_edit.height()
        assert (
            abs(eye.geometry().center().y() - window.password_edit.rect().center().y())
            <= 1
        )
        assert window.old_preview.height() >= 200
        assert window.new_preview.height() >= 200
    finally:
        window.deleteLater()
        app.processEvents()
        app.setStyleSheet(previous_style)


def test_advanced_settings_arrows_are_styled_and_operable():
    app = QApplication.instance() or QApplication([])
    previous_style = app.styleSheet()
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    dialog = AdvancedSettingsDialog(AppSettings())
    try:
        assert (ARROW_DIRECTORY / "chevron-up.png").is_file()
        assert (ARROW_DIRECTORY / "chevron-down.png").is_file()
        dialog.show()
        app.processEvents()

        spin = dialog.port_spin
        original_port = spin.value()
        QTest.mouseClick(
            spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width() - 12, 7)
        )
        assert spin.value() == original_port + 1
        QTest.mouseClick(
            spin,
            Qt.MouseButton.LeftButton,
            pos=QPoint(spin.width() - 12, spin.height() - 7),
        )
        assert spin.value() == original_port

        combo = dialog.auth_combo
        QTest.mouseClick(
            combo,
            Qt.MouseButton.LeftButton,
            pos=QPoint(combo.width() - 12, combo.height() // 2),
        )
        popup = combo.popup
        view = popup.list_view
        assert popup.isVisible()
        assert popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert popup.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert popup.windowFlags() & Qt.WindowType.NoDropShadowWindowHint
        assert (
            popup.windowFlags() & Qt.WindowType.WindowType_Mask
        ) == Qt.WindowType.Tool
        assert popup.width() == combo.width()
        assert popup.y() == combo.mapToGlobal(QPoint(0, combo.height() + 1)).y()
        assert combo.screen().availableGeometry().contains(popup.geometry())
        image = popup.grab().toImage()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(image.width() - 1, image.height() - 1).alpha() == 0
        assert image.pixelColor(image.width() // 2, 0).alpha() > 0
        assert image.pixelColor(image.width() // 2, image.height() - 1).alpha() > 0
        assert popup.mask().isEmpty()
        first_row = view.visualRect(combo.model().index(0, 0))
        second_row = view.visualRect(combo.model().index(1, 0))
        assert first_row.height() == second_row.height() == 32
        assert first_row.bottom() + 1 == second_row.top()
        outside = view.viewport().mapTo(popup, QPoint(1, first_row.center().y()))
        inside = view.viewport().mapTo(
            popup, QPoint(first_row.width() - 8, first_row.center().y())
        )
        assert image.pixelColor(outside).name() == "#ffffff"
        assert image.pixelColor(inside).name() == "#e8f1f8"
        QTest.qWait(1200)
        QTest.mouseClick(
            combo,
            Qt.MouseButton.LeftButton,
            pos=QPoint(combo.width() - 12, combo.height() // 2),
        )
        assert not popup.isVisible()
        QTest.mouseClick(
            combo,
            Qt.MouseButton.LeftButton,
            pos=QPoint(combo.width() - 12, combo.height() // 2),
        )
        assert popup.isVisible()
        QTest.mouseClick(
            view.viewport(), Qt.MouseButton.LeftButton, pos=second_row.center()
        )
        assert combo.currentData() == "key"
        assert not popup.isVisible()
        combo.showPopup()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        assert not popup.isVisible()
        combo.showPopup()
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Return)
        assert combo.currentData() == "password"
        assert not popup.isVisible()
        combo.showPopup()
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
        assert not popup.isVisible()
    finally:
        dialog.auth_combo.hidePopup()
        dialog.deleteLater()
        app.processEvents()
        app.setStyleSheet(previous_style)


def test_preview_scrollbar_uses_compact_style_and_still_scrolls(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    previous_style = app.styleSheet()
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow(PortableSettingsStore(tmp_path / "settings.ini"))
    try:
        window.resize(960, 720)
        window.old_preview.setPlainText(
            "\n".join(f"server line {index}" for index in range(100))
        )
        window.show()
        app.processEvents()

        scrollbar = window.old_preview.verticalScrollBar()
        assert scrollbar.isVisible()
        assert scrollbar.width() == 12
        assert scrollbar.maximum() > 0
        QTest.mouseClick(
            scrollbar,
            Qt.MouseButton.LeftButton,
            pos=QPoint(scrollbar.width() // 2, scrollbar.height() - 15),
        )
        assert scrollbar.value() > 0
    finally:
        window.deleteLater()
        app.processEvents()
        app.setStyleSheet(previous_style)
