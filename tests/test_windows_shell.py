from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from singroute.application.app_update import (
    _powershell_executable,
    _read_installer_script,
)
from singroute.infrastructure import windows_shell


def test_executable_refresh_reaches_windows_shell(tmp_path: Path, shell_change_events):
    target = tmp_path / "Программа.exe"
    target.write_bytes(b"MZtest")

    windows_shell.notify_executable_changed(target)

    assert target in shell_change_events()


def test_shell_failure_does_not_prevent_startup(monkeypatch: pytest.MonkeyPatch):
    def unavailable_shell(*args):
        raise OSError("Shell unavailable")

    monkeypatch.setattr(windows_shell.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_shell.ctypes, "WinDLL", unavailable_shell, raising=False
    )

    windows_shell.notify_executable_changed(Path("SingRoute.exe"))


def test_powershell_shell_notification(tmp_path: Path, shell_change_events):
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZtest")
    source_path = tmp_path / "updater.ps1"
    source_path.write_text(_read_installer_script(), encoding="utf-8-sig")
    script = tmp_path / "notify.ps1"
    script.write_text(
        """param([string]$TargetPath, [string]$SourcePath)
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $SourcePath, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count) { throw $parseErrors[0] }
$definition = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Update-ExecutableShellIcon'
}, $true)
. ([scriptblock]::Create($definition.Extent.Text))
Update-ExecutableShellIcon
Update-ExecutableShellIcon
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            str(_powershell_executable()),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-TargetPath",
            str(target),
            "-SourcePath",
            str(source_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert target in shell_change_events()
