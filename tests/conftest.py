from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path
from threading import Event, Thread

import pytest


@pytest.fixture(scope="session")
def _shell_notification_listener(tmp_path_factory):
    """Listen for real Windows shell notifications without showing a window."""
    if sys.platform != "win32":
        pytest.skip("Windows shell integration")
    test_root = tmp_path_factory.getbasetemp()
    user32 = ctypes.WinDLL("user32")
    shell32 = ctypes.WinDLL("shell32")
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class Entry(ctypes.Structure):
        _fields_ = [("pidl", ctypes.c_void_p), ("recursive", wintypes.BOOL)]

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    set_window_proc = (
        user32.SetWindowLongPtrW
        if ctypes.sizeof(ctypes.c_void_p) == 8
        else user32.SetWindowLongW
    )
    set_window_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_window_proc.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHChangeNotifyRegister.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.LONG,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.POINTER(Entry),
    ]
    shell32.SHChangeNotifyDeregister.argtypes = [wintypes.ULONG]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHChangeNotification_Lock.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wintypes.LONG),
    ]
    shell32.SHChangeNotification_Lock.restype = wintypes.HANDLE
    shell32.SHChangeNotification_Unlock.argtypes = [wintypes.HANDLE]
    events: list[Path] = []
    message_id = 0x8001

    @callback_type
    def window_proc(hwnd, message, wparam, lparam):
        if message == message_id:
            pidls = ctypes.POINTER(ctypes.c_void_p)()
            event = wintypes.LONG()
            lock = shell32.SHChangeNotification_Lock(
                wparam, lparam, ctypes.byref(pidls), ctypes.byref(event)
            )
            if lock:
                try:
                    buffer = ctypes.create_unicode_buffer(260)
                    if event.value & 0x3000 and shell32.SHGetPathFromIDListW(
                        pidls[0], buffer
                    ):
                        events.append(Path(buffer.value))
                finally:
                    shell32.SHChangeNotification_Unlock(lock)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    ready = Event()
    stop = Event()

    def listen():
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "", 0, 0, 0, 0, 0, None, None, None, None
        )
        assert hwnd
        set_window_proc(hwnd, -4, ctypes.cast(window_proc, ctypes.c_void_p).value)
        pidl = shell32.ILCreateFromPathW(str(test_root))
        assert pidl
        entry = Entry(pidl, True)
        # Shell-level notifications with shared-memory delivery across processes.
        registration = shell32.SHChangeNotifyRegister(
            hwnd, 0x8002, 0x3000, message_id, 1, ctypes.byref(entry)
        )
        assert registration
        try:
            ready.set()
            message = wintypes.MSG()
            while not stop.is_set():
                while user32.PeekMessageW(ctypes.byref(message), hwnd, 0, 0, 1):
                    user32.DispatchMessageW(ctypes.byref(message))
                stop.wait(0.01)
        finally:
            shell32.SHChangeNotifyDeregister(registration)
            shell32.ILFree(pidl)
            user32.DestroyWindow(hwnd)

    listener = Thread(target=listen, daemon=True)
    listener.start()
    assert ready.wait(5), "Shell listener did not start"

    try:
        yield events
    finally:
        stop.set()
        listener.join(timeout=5)
        assert not listener.is_alive()


@pytest.fixture
def shell_change_events(tmp_path: Path, _shell_notification_listener):
    # Keep the thread that registered with shell32 alive throughout the session:
    # shell32 retains notification machinery bound to that thread.
    def wait_for_events(expected_count: int = 1) -> list[Path]:
        deadline = time.monotonic() + 5
        while True:
            matches = [
                path
                for path in _shell_notification_listener
                if path == tmp_path or path.parent == tmp_path
            ]
            if len(matches) >= expected_count or time.monotonic() >= deadline:
                return matches
            time.sleep(0.02)

    return wait_for_events
