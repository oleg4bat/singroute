"""Small controls that share the SingRoute line-icon style."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_INK = QColor("#526B80")


def _line_icon(draw: Callable[[QPainter], None]) -> QIcon:
    icon = QIcon()
    for size in (20, 40):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 20, size / 20)
        pen = QPen(_INK, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        draw(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def visibility_icon(password_visible: bool) -> QIcon:
    def draw(painter: QPainter) -> None:
        eye = QPainterPath()
        eye.moveTo(2.0, 10.0)
        eye.cubicTo(5.2, 4.7, 14.8, 4.7, 18.0, 10.0)
        eye.cubicTo(14.8, 15.3, 5.2, 15.3, 2.0, 10.0)
        painter.drawPath(eye)
        painter.drawEllipse(QPointF(10.0, 10.0), 2.3, 2.3)
        if password_visible:
            painter.drawLine(QPointF(3.0, 17.0), QPointF(17.0, 3.0))

    return _line_icon(draw)


def settings_icon() -> QIcon:
    def draw(painter: QPainter) -> None:
        painter.drawLine(QPointF(3.0, 6.0), QPointF(5.0, 6.0))
        painter.drawLine(QPointF(9.0, 6.0), QPointF(17.0, 6.0))
        painter.drawLine(QPointF(3.0, 14.0), QPointF(11.0, 14.0))
        painter.drawLine(QPointF(15.0, 14.0), QPointF(17.0, 14.0))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QPointF(7.0, 6.0), 2.0, 2.0)
        painter.drawEllipse(QPointF(13.0, 14.0), 2.0, 2.0)

    return _line_icon(draw)
