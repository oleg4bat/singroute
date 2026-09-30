"""Refresh Explorer's cached information for a replaced portable executable."""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def notify_executable_changed(path: Path) -> None:
    """Request a refresh for this file without clearing the system icon cache."""
    if sys.platform != "win32":
        return
    try:
        notify = ctypes.WinDLL("shell32").SHChangeNotify
        notify.argtypes = [
            ctypes.c_long,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
        ]
        notify.restype = None
        # SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSHNOWAIT.
        notify(0x2000, 0x0005 | 0x2000, str(path.resolve()), None)
    except OSError:
        # Cosmetic refresh must not prevent startup or update health reporting.
        _LOGGER.debug("Could not notify the Windows shell", exc_info=True)
