"""SingRoute desktop identity and widget styling."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

ICON_PATH = Path(__file__).resolve().parents[1] / "assets" / "singroute.png"
ARROW_DIRECTORY = ICON_PATH.parent


def brand_icon() -> QIcon:
    return QIcon(str(ICON_PATH))


class RoundedComboItemDelegate(QStyledItemDelegate):
    """Paint the popup selection without the square platform focus rectangle."""

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(32, hint.height()))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e8f1f8" if selected else "#f1f6fa"))
            painter.drawRoundedRect(option.rect.adjusted(5, 2, -5, -2), 6, 6)
            painter.restore()

        text_option = QStyleOptionViewItem(option)
        text_option.state &= ~(
            QStyle.StateFlag.State_Selected
            | QStyle.StateFlag.State_MouseOver
            | QStyle.StateFlag.State_HasFocus
        )
        super().paint(painter, text_option, index)


class RoundedComboPopup(QFrame):
    """One painted surface that does not auto-dismiss before a combo click."""

    def __init__(self, combo: RoundedComboBox) -> None:
        super().__init__(
            combo,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.combo = combo
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.list_view = QListView(self)
        self.list_view.setStyleSheet(
            "QListView { background: transparent; border: 0; outline: 0; }"
        )
        self.list_view.setItemDelegate(RoundedComboItemDelegate(self.list_view))
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)
        self.list_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_view.clicked.connect(self._choose)
        self.list_view.activated.connect(self._choose)
        self.list_view.installEventFilter(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.list_view)
        QApplication.instance().applicationStateChanged.connect(
            self._on_application_state_changed
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def hideEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d6e0eb"), 1))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8
        )

    def eventFilter(self, watched, event) -> bool:
        if watched is self.list_view and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._choose(self.list_view.currentIndex())
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                self.combo.setFocus()
                return True
        if (
            self.isVisible()
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(watched, QWidget)
            and watched is not self.combo
            and not self.combo.isAncestorOf(watched)
            and watched is not self
            and not self.isAncestorOf(watched)
        ):
            self.hide()
        return super().eventFilter(watched, event)

    def _on_application_state_changed(self, state: Qt.ApplicationState) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self.hide()

    def _choose(self, index: QModelIndex) -> None:
        if index.isValid() and self.isVisible():
            self.combo.setCurrentIndex(index.row())
            self.combo.activated.emit(index.row())
        self.hide()
        self.combo.setFocus()


class RoundedComboBox(QComboBox):
    """Show a compact, consistently rounded dropdown below the field."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.popup = RoundedComboPopup(self)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.popup.isVisible():
            self.hidePopup()
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def showPopup(self) -> None:
        if self.popup.isVisible():
            self.hidePopup()
            self.setFocus()
            return
        if not self.count():
            return
        popup = self.popup
        popup.list_view.setModel(self.model())
        current = self.model().index(self.currentIndex(), self.modelColumn())
        popup.list_view.setCurrentIndex(current)
        popup.resize(self.width(), min(self.count(), 8) * 32 + 12)
        bounds = self.screen().availableGeometry()
        below = self.mapToGlobal(QPoint(0, self.height() + 1))
        above = self.mapToGlobal(QPoint(0, -popup.height() - 1))
        x = max(bounds.left(), min(below.x(), bounds.right() - popup.width() + 1))
        y = (
            below.y()
            if below.y() + popup.height() <= bounds.bottom() + 1
            else above.y()
        )
        y = max(bounds.top(), min(y, bounds.bottom() - popup.height() + 1))
        popup.move(x, y)
        popup.show()
        popup.list_view.setFocus()

    def hidePopup(self) -> None:
        self.popup.hide()


APP_STYLESHEET = """
QWidget {
    color: #183047;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QMainWindow, QDialog { background: #f3f6fa; }
QLabel#title { color: #142c49; font-size: 24px; font-weight: 700; }
QLabel#subtitle, QLabel#mutedLabel { color: #64748b; }
QLabel#connectedLabel {
    color: #126b57; background: #e8f7f1; border-radius: 7px;
    padding: 5px 9px; font-weight: 600;
}
QLabel#disconnectedLabel {
    color: #a13735; background: #fff0ee; border-radius: 7px;
    padding: 5px 9px; font-weight: 600;
}
QGroupBox {
    background: #ffffff; border: 1px solid #dfe6ef; border-radius: 14px;
    margin-top: 9px; padding: 10px 12px 8px;
    color: #142c49; font-size: 13px; font-weight: 650;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 14px; padding: 0 6px; background: #ffffff;
}
QPushButton {
    background: #ffffff; border: 1px solid #d5dfeb; border-radius: 8px;
    padding: 6px 11px; min-height: 18px;
}
QPushButton:hover { background: #edf4fa; border-color: #a9bfd5; }
QPushButton:pressed { background: #e1ebf5; }
QPushButton:disabled { color: #94a3b8; background: #f3f6fa; border-color: #e2e8f0; }
QPushButton#primaryButton {
    background: #2a5ed4; border-color: #2a5ed4; color: #ffffff; font-weight: 600;
}
QPushButton#primaryButton:hover { background: #214fb8; border-color: #214fb8; }
QPushButton#applyButton {
    background: #176f71; border-color: #176f71; color: #ffffff; font-weight: 650;
    padding-left: 18px; padding-right: 18px;
}
QPushButton#applyButton:hover { background: #115e60; border-color: #115e60; }
QPushButton#primaryButton:disabled, QPushButton#applyButton:disabled {
    background: #b6c4d1; border-color: #b6c4d1; color: #ffffff;
}
QToolButton#settingsButton {
    background: #ffffff; border: 1px solid #d5dfeb; border-radius: 8px;
    padding: 0;
}
QToolButton#settingsButton:hover { background: #edf4fa; border-color: #a9bfd5; }
QLineEdit, QPlainTextEdit {
    background: #f9fbfd; border: 1px solid #d6e0eb; border-radius: 8px;
    padding: 6px 8px; selection-background-color: #2a5ed4;
}
QComboBox, QSpinBox {
    background: #f9fbfd; border: 1px solid #d6e0eb; border-radius: 8px;
    padding: 5px 30px 5px 9px; selection-background-color: #2a5ed4;
}
QComboBox {
    selection-background-color: #e8f1f8; selection-color: #142c49;
}
QComboBox::drop-down {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 27px; border: 0; background: transparent;
}
QComboBox::down-arrow {
    image: url("__DOWN_ARROW__"); width: 14px; height: 14px;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 25px; border: 0; background: transparent;
}
QSpinBox::up-button { subcontrol-position: top right; height: 15px; }
QSpinBox::down-button { subcontrol-position: bottom right; height: 15px; }
QSpinBox::up-arrow {
    image: url("__UP_ARROW__"); width: 12px; height: 12px;
}
QSpinBox::down-arrow {
    image: url("__DOWN_ARROW__"); width: 12px; height: 12px;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    background: #ffffff; border-color: #2a5ed4;
}
QPlainTextEdit { font-family: Consolas, monospace; font-size: 9pt; }
QScrollBar:vertical {
    background: #f7fafc; border: 0; border-radius: 6px;
    width: 12px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #aebfd0; border-radius: 5px; min-height: 28px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background: #829bb1; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    border: 0; background: transparent; height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: #f7fafc; border: 0; border-radius: 6px;
    height: 12px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #aebfd0; border-radius: 5px; min-width: 28px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background: #829bb1; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    border: 0; background: transparent; width: 0;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
"""
APP_STYLESHEET = APP_STYLESHEET.replace(
    "__UP_ARROW__", (ARROW_DIRECTORY / "chevron-up.png").as_posix()
).replace("__DOWN_ARROW__", (ARROW_DIRECTORY / "chevron-down.png").as_posix())
