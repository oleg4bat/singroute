"""Render the editable SingRoute mark into runtime and Windows EXE icons."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)
    assets = Path(__file__).resolve().parents[1] / "singroute" / "assets"
    renderer = QSvgRenderer(str(assets / "singroute.svg"))
    if not renderer.isValid():
        raise RuntimeError("Brand SVG could not be rendered")

    entries: list[tuple[int, bytes]] = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        canvas = QImage(size, size, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not canvas.save(buffer, "PNG"):
            raise RuntimeError(f"Could not render {size}px brand icon")
        entries.append((size, bytes(data)))
        if size == 256:
            (assets / "singroute.png").write_bytes(bytes(data))

    offset = 6 + 16 * len(entries)
    directory = [struct.pack("<HHH", 0, 1, len(entries))]
    images = []
    for size, data in entries:
        directory.append(
            struct.pack(
                "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
            )
        )
        images.append(data)
        offset += len(data)
    (assets / "singroute.ico").write_bytes(b"".join(directory + images))

    for direction in ("up", "down"):
        arrow = QSvgRenderer(str(assets / f"chevron-{direction}.svg"))
        if not arrow.isValid():
            raise RuntimeError(f"Could not render {direction} arrow")
        canvas = QImage(16, 16, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        arrow.render(painter)
        painter.end()
        if not canvas.save(str(assets / f"chevron-{direction}.png")):
            raise RuntimeError(f"Could not save {direction} arrow")
    app.quit()


if __name__ == "__main__":
    main()
