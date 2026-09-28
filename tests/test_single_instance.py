from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

import singroute.gui.app as gui_app


@pytest.mark.parametrize(
    ("state", "expected_show"),
    [
        (Qt.WindowState.WindowMinimized, "normal"),
        (
            Qt.WindowState.WindowMinimized | Qt.WindowState.WindowMaximized,
            "maximized",
        ),
    ],
)
def test_activation_restores_minimized_window(state, expected_show, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        gui_app,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(
                user32=SimpleNamespace(
                    SetForegroundWindow=lambda handle: calls.append(
                        f"foreground:{handle}"
                    )
                )
            )
        ),
    )

    class Window:
        def isMinimized(self) -> bool:
            return True

        def windowState(self):
            return state

        def showNormal(self) -> None:
            calls.append("normal")

        def showMaximized(self) -> None:
            calls.append("maximized")

        def raise_(self) -> None:
            calls.append("raised")

        def activateWindow(self) -> None:
            calls.append("activated")

        def winId(self) -> int:
            return 123

    gui_app._activate_window(Window())  # type: ignore[arg-type]

    expected = [expected_show, "raised", "activated"]
    if sys.platform == "win32":
        expected.append("foreground:123")
    assert calls == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows foreground API")
def test_second_instance_grants_foreground_permission_to_lock_owner(
    tmp_path: Path, monkeypatch
):
    calls: list[int] = []
    monkeypatch.setattr(
        gui_app,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(
                user32=SimpleNamespace(AllowSetForegroundWindow=calls.append)
            )
        ),
    )
    lock = gui_app._acquire_instance_lock(tmp_path)
    assert lock is not None
    try:
        gui_app._allow_existing_process_to_focus(tmp_path)
    finally:
        lock.unlock()

    assert calls == [os.getpid()]


def test_second_process_activates_existing_window(tmp_path: Path):
    ready = tmp_path / "ready"
    activated = tmp_path / "activated"
    directory = str(tmp_path)
    primary_script = """
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
import singroute.gui.app as app
from singroute.gui.main_window import MainWindow
from singroute.infrastructure.settings import AppSettings, PortableSettingsStore

directory, ready, activated = map(Path, sys.argv[1:4])
app._application_directory = lambda: directory
app.signal_update_health = lambda: None
app.retained_update_backup_path = lambda: None
settings_store = PortableSettingsStore(directory / 'settings.ini')
settings_store.save(AppSettings(check_updates_on_startup=False))

class Window(MainWindow):
    def __init__(self):
        super().__init__(settings_store)

    def show(self):
        super().show()
        self.showMinimized()

app.MainWindow = Window
original_server = app._start_activation_server
original_activate = app._activate_window
activations = []

def start_server(window, path):
    server = original_server(window, path)
    ready.write_text('ready', encoding='utf-8')
    return server

def activate(window):
    original_activate(window)
    activations.append(str(window.isMinimized()))
    activated.write_text('\\n'.join(activations), encoding='utf-8')
    if len(activations) == 2:
        QApplication.instance().quit()
    else:
        window.showMinimized()

app._start_activation_server = start_server
app._activate_window = activate
raise SystemExit(app.run_gui())
"""
    secondary_script = """
import sys
from pathlib import Path
import singroute.gui.app as app

app._application_directory = lambda: Path(sys.argv[1])
def unexpected_dialog(*_args):
    raise AssertionError('The existing window was not reached')

app.QMessageBox.warning = unexpected_dialog
app.QMessageBox.information = unexpected_dialog

def unexpected_window():
    raise AssertionError('A second window was created')

app.MainWindow = unexpected_window
raise SystemExit(app.run_gui())
"""
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    project_root = Path(__file__).resolve().parents[1]
    primary = subprocess.Popen(
        [sys.executable, "-c", primary_script, directory, str(ready), str(activated)],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and primary.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert ready.exists(), primary.communicate(timeout=2)

        for _ in range(2):
            secondary = subprocess.run(
                [sys.executable, "-c", secondary_script, directory],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert secondary.returncode == 0, secondary.stderr
        stdout, stderr = primary.communicate(timeout=10)
        assert primary.returncode == 0, f"{stdout}\n{stderr}"
        assert activated.read_text(encoding="utf-8") == "False\nFalse"
    finally:
        if primary.poll() is None:
            primary.kill()
            primary.communicate(timeout=10)
