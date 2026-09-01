# -*- mode: python ; coding: utf-8 -*-

import re
from pathlib import Path, PurePath

from PyInstaller.utils.hooks import copy_metadata


project_root = Path(SPECPATH).parent.resolve()

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (
            str(project_root / "singroute" / "application" / "update.ps1"),
            "singroute/application",
        ),
        *copy_metadata("singroute"),
    ],
    hiddenimports=["keyring.backends.Windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)


def is_external_icu_binary(entry: tuple[str, str, str]) -> bool:
    destination = PurePath(entry[0])
    if len(destination.parts) != 1:
        return False
    filename = destination.name.casefold()
    return filename == "icuuc.dll" or re.fullmatch(r"icudt\d+\.dll", filename) is not None


# Qt for Windows uses the system ICU shim. Developer PATH entries (for example,
# Poppler) can contain an incompatible DLL with the same name; PyInstaller would
# otherwise collect it and make QtCore fail before the GUI starts.
a.binaries = [entry for entry in a.binaries if not is_external_icu_binary(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SingRoute",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
