from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from singroute.application import app_update
from singroute.application.app_update import (
    EXECUTABLE_ASSET_NAME,
    LATEST_RELEASE_API,
    MAX_METADATA_BYTES,
    UPDATE_HEALTH_FILENAME,
    AppUpdateError,
    ReleaseInfo,
    StagedUpdate,
    _powershell_executable,
    _write_installer_script,
    check_for_update,
    launch_staged_update,
    retained_update_backup_path,
    signal_update_health,
    stage_update,
    take_update_error,
)


class FakeResponse(BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> FakeResponse:
        assert timeout > 0
        self.requests.append(request)
        return FakeResponse(self.responses[request.full_url])


def test_check_for_update_finds_newer_official_release():
    executable = b"MZrelease"
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.3.3", executable)})

    result = check_for_update("0.3.2", opener=opener)

    assert result.update_available is True
    assert result.latest_release.version == "0.3.3"
    assert result.latest_release.tag == "v0.3.3"
    assert (
        result.latest_release.executable_digest
        == hashlib.sha256(executable).hexdigest()
    )
    assert opener.requests[0].get_header("User-agent") == "SingRoute-Updater"


def test_check_for_update_does_not_downgrade():
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.3.2", b"MZrelease")})

    result = check_for_update("0.3.3", opener=opener)

    assert result.update_available is False


def test_check_rejects_release_assets_from_unexpected_location():
    payload = json.loads(_release_json("v0.3.3", b"MZrelease"))
    payload["assets"][0]["browser_download_url"] = "https://example.test/SingRoute.exe"
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match=r"отсутствует SingRoute\.exe"):
        check_for_update("0.3.2", opener=opener)


def test_check_rejects_release_page_from_unexpected_location():
    payload = json.loads(_release_json("v0.3.3", b"MZrelease"))
    payload["html_url"] = "https://example.test/releases/tag/v0.3.3"
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="неожиданный адрес"):
        check_for_update("0.3.2", opener=opener)


def test_check_rejects_non_semantic_release_tag():
    payload = json.loads(_release_json("v0.3.3", b"MZrelease"))
    payload["tag_name"] = "latest"
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="формат"):
        check_for_update("0.3.2", opener=opener)


def test_check_rejects_oversized_metadata():
    opener = FakeOpener({LATEST_RELEASE_API: b"x" * (MAX_METADATA_BYTES + 1)})

    with pytest.raises(AppUpdateError, match="превышает"):
        check_for_update("0.3.2", opener=opener)


def test_check_rejects_release_without_github_digest():
    payload = json.loads(_release_json("v0.3.3", b"MZrelease"))
    payload["assets"][0]["digest"] = None
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="digest"):
        check_for_update("0.3.2", opener=opener)


def test_stage_update_verifies_github_digest_and_keeps_verified_file(
    tmp_path: Path,
):
    executable = b"MZsigned portable executable"
    digest = hashlib.sha256(executable).hexdigest()
    release = _release_info("0.3.3", digest)
    opener = FakeOpener({release.executable_url: executable})
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZold executable")

    staged = stage_update(release, target, opener=opener)

    assert staged.target_path == target.resolve()
    assert staged.executable_path.read_bytes() == executable
    assert staged.expected_digest == digest
    assert target.read_bytes() == b"MZold executable"
    assert [request.full_url for request in opener.requests] == [release.executable_url]


def test_stage_update_removes_download_with_wrong_digest(tmp_path: Path):
    expected = b"MZexpected"
    digest = hashlib.sha256(expected).hexdigest()
    release = _release_info("0.3.3", digest)
    opener = FakeOpener({release.executable_url: b"MZtampered"})
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZold executable")

    with pytest.raises(AppUpdateError, match="целостности"):
        stage_update(release, target, opener=opener)

    assert target.read_bytes() == b"MZold executable"
    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob(".*.update-*.exe")) == []


def test_updated_gui_reports_health_with_one_time_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = tmp_path / "SingRoute.exe"
    health_path = tmp_path / UPDATE_HEALTH_FILENAME
    token = "a" * 64
    monkeypatch.setattr(app_update.sys, "executable", str(executable))
    monkeypatch.setenv("SINGROUTE_UPDATE_HEALTH_PATH", str(health_path))
    monkeypatch.setenv("SINGROUTE_UPDATE_HEALTH_TOKEN", token)

    assert signal_update_health() is True

    assert health_path.read_text(encoding="ascii") == token
    assert "SINGROUTE_UPDATE_HEALTH_PATH" not in os.environ
    assert "SINGROUTE_UPDATE_HEALTH_TOKEN" not in os.environ


def test_updated_gui_rejects_health_path_outside_executable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = tmp_path / "SingRoute.exe"
    unexpected_path = tmp_path / "elsewhere" / UPDATE_HEALTH_FILENAME
    monkeypatch.setattr(app_update.sys, "executable", str(executable))
    monkeypatch.setenv("SINGROUTE_UPDATE_HEALTH_PATH", str(unexpected_path))
    monkeypatch.setenv("SINGROUTE_UPDATE_HEALTH_TOKEN", "b" * 64)

    assert signal_update_health() is False
    assert unexpected_path.exists() is False


def test_take_update_error_returns_message_once(tmp_path: Path):
    target = tmp_path / "SingRoute.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    error_path.write_text("new version exited", encoding="utf-8")

    assert take_update_error(target) == "new version exited"
    assert take_update_error(target) is None


def test_retained_update_backup_is_reported_only_when_present(tmp_path: Path):
    target = tmp_path / "SingRoute.exe"

    assert retained_update_backup_path(target) is None

    backup = tmp_path / ".SingRoute.previous.exe"
    backup.write_bytes(b"MZprevious")
    assert retained_update_backup_path(target) == backup.resolve()


def test_launch_staged_update_hands_resolved_paths_to_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    staged = _staged_update(tmp_path)
    script_path = tmp_path / "installer.ps1"
    script_path.write_text("# installer", encoding="utf-8")
    powershell_path = tmp_path / "powershell.exe"
    captured: dict[str, object] = {}
    dll_directory_calls: list[str | None] = []

    class ReadyProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs
            ready_path = Path(command[command.index("-ReadyPath") + 1])
            ready_path.write_text("ready", encoding="utf-8")

        def poll(self) -> None:
            return None

    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.os, "getppid", lambda: 3131)
    monkeypatch.setattr(
        app_update.sys,
        "_MEIPASS",
        str(tmp_path / "_MEI-old"),
        raising=False,
    )
    monkeypatch.setattr(app_update, "_powershell_executable", lambda: powershell_path)
    monkeypatch.setattr(app_update, "_write_installer_script", lambda: script_path)
    monkeypatch.setattr(
        app_update,
        "_set_windows_dll_directory",
        dll_directory_calls.append,
    )
    monkeypatch.setattr(app_update.subprocess, "Popen", ReadyProcess)

    launch_staged_update(staged, process_id=4242)

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(powershell_path)
    assert command[command.index("-SingRouteProcessId") + 1] == "4242"
    assert command[command.index("-SingRouteParentProcessId") + 1] == "3131"
    assert command[command.index("-StagedPath") + 1] == str(
        staged.executable_path.resolve()
    )
    assert command[command.index("-TargetPath") + 1] == str(
        staged.target_path.resolve()
    )
    health_path = staged.target_path.with_name(UPDATE_HEALTH_FILENAME).resolve()
    assert command[command.index("-HealthPath") + 1] == str(health_path)
    assert len(command[command.index("-HealthToken") + 1]) == 64
    assert captured["kwargs"] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }
    assert dll_directory_calls == [None, str(tmp_path / "_MEI-old")]
    assert (
        app_update._read_installer_script().count(
            '$env:PYINSTALLER_RESET_ENVIRONMENT = "1"'
        )
        == 2
    )


def test_launch_staged_update_reports_helper_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    staged = _staged_update(tmp_path)
    script_path = tmp_path / "installer.ps1"
    script_path.write_text("# installer", encoding="utf-8")

    class FailedProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            error_path = Path(command[command.index("-ErrorPath") + 1])
            error_path.write_text("helper diagnostics", encoding="utf-8")

        def poll(self) -> int:
            return 17

    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(
        app_update, "_powershell_executable", lambda: tmp_path / "pwsh.exe"
    )
    monkeypatch.setattr(app_update, "_write_installer_script", lambda: script_path)
    monkeypatch.setattr(app_update.subprocess, "Popen", FailedProcess)

    with pytest.raises(AppUpdateError, match="helper diagnostics"):
        launch_staged_update(staged)

    assert script_path.exists() is False
    assert (tmp_path / "SingRoute-update-error.txt").exists() is False


def test_launch_checks_powershell_before_creating_temporary_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    staged = _staged_update(tmp_path)
    script_created = False

    def fail_powershell_lookup() -> Path:
        raise AppUpdateError("PowerShell не найден")

    def create_script() -> Path:
        nonlocal script_created
        script_created = True
        return tmp_path / "installer.ps1"

    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(app_update, "_powershell_executable", fail_powershell_lookup)
    monkeypatch.setattr(app_update, "_write_installer_script", create_script)

    with pytest.raises(AppUpdateError, match="PowerShell не найден"):
        launch_staged_update(staged)

    assert script_created is False


def test_launch_staged_update_stops_unresponsive_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    staged = _staged_update(tmp_path)
    script_path = tmp_path / "installer.ps1"
    script_path.write_text("# installer", encoding="utf-8")

    class StuckProcess:
        terminated = False
        killed = False
        wait_calls = 0

        def __init__(self, command: list[str], **kwargs: object) -> None:
            pass

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: int) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("powershell", timeout)

    process: StuckProcess | None = None

    def start_process(command: list[str], **kwargs: object) -> StuckProcess:
        nonlocal process
        process = StuckProcess(command, **kwargs)
        return process

    monotonic_values = iter([100.0, 106.0])
    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(
        app_update, "_powershell_executable", lambda: tmp_path / "pwsh.exe"
    )
    monkeypatch.setattr(app_update, "_write_installer_script", lambda: script_path)
    monkeypatch.setattr(app_update.subprocess, "Popen", start_process)
    monkeypatch.setattr(app_update.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(AppUpdateError, match="не подтвердил запуск"):
        launch_staged_update(staged)

    assert process is not None
    assert process.terminated is True
    assert process.killed is True
    assert script_path.exists() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_retries_until_locked_target_can_be_replaced(
    tmp_path: Path,
    shell_change_events,
):
    powershell = _powershell_executable()
    staged = tmp_path / ".SingRoute.update-v0.3.3.cmd"
    target = tmp_path / "SingRoute.cmd"
    backup = tmp_path / ".SingRoute.previous.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    health_path = tmp_path / UPDATE_HEALTH_FILENAME
    health_token = "c" * 64
    staged.write_text(
        "@echo off\n"
        '<nul set /p "=%SINGROUTE_UPDATE_HEALTH_TOKEN%" '
        '> "%SINGROUTE_UPDATE_HEALTH_PATH%"\n'
        "ping 127.0.0.1 -n 8 > nul\n",
        encoding="ascii",
    )
    target.write_text("@echo off\nexit /b 0\n", encoding="ascii")
    expected_digest = hashlib.sha256(staged.read_bytes()).hexdigest()

    lock_ready_path = tmp_path / "locked.ready"
    locker_script = tmp_path / "hold-lock.ps1"
    locker_script.write_text(
        r"""param([string]$TargetPath, [string]$ReadyPath)
$stream = [System.IO.File]::Open(
    $TargetPath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::None
)
try {
    [System.IO.File]::WriteAllText($ReadyPath, "ready")
    Start-Sleep -Milliseconds 1500
}
finally {
    $stream.Dispose()
}
""",
        encoding="utf-8-sig",
    )
    locker = subprocess.Popen(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(locker_script),
            "-TargetPath",
            str(target),
            "-ReadyPath",
            str(lock_ready_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not lock_ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lock_ready_path.exists(), locker.stderr.read() if locker.stderr else ""

    script = _write_installer_script()
    started_at = time.monotonic()
    result = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SingRouteProcessId",
            "2147483647",
            "-StagedPath",
            str(staged),
            "-TargetPath",
            str(target),
            "-BackupPath",
            str(backup),
            "-ErrorPath",
            str(error_path),
            "-ReadyPath",
            str(ready_path),
            "-HealthPath",
            str(health_path),
            "-HealthToken",
            health_token,
            "-ScriptPath",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    elapsed = time.monotonic() - started_at
    locker.wait(timeout=10)

    assert result.returncode == 0, result.stderr
    assert elapsed >= 1.0
    assert staged.exists() is False
    assert backup.exists() is False
    assert error_path.exists() is False
    assert ready_path.exists() is False
    assert health_path.exists() is False
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_digest
    assert any(path in (target, tmp_path) for path in shell_change_events())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_does_not_start_second_instance_when_parent_stays_alive(
    tmp_path: Path,
):
    powershell = _powershell_executable()
    previous_probe = _compile_health_probe(tmp_path)
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(previous_probe.read_bytes())
    staged = tmp_path / ".SingRoute.update-v0.4.0.exe"
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", staged)
    backup = tmp_path / ".SingRoute.previous.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    health_path = tmp_path / UPDATE_HEALTH_FILENAME
    previous_started_path = tmp_path / ".SingRoute.previous-started"
    script = _write_installer_script()

    result = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SingRouteProcessId",
            str(os.getpid()),
            "-StagedPath",
            str(staged),
            "-TargetPath",
            str(target),
            "-BackupPath",
            str(backup),
            "-ErrorPath",
            str(error_path),
            "-ReadyPath",
            str(ready_path),
            "-HealthPath",
            str(health_path),
            "-HealthToken",
            "d" * 64,
            "-ScriptPath",
            str(script),
            "-ExitTimeoutSeconds",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert target.read_bytes() == previous_probe.read_bytes()
    assert staged.exists() is True
    assert backup.exists() is False
    assert previous_started_path.exists() is False
    error = error_path.read_text(encoding="utf-8")
    assert "did not exit" in error
    assert "previous version was not restarted" not in error


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_refuses_update_while_another_target_instance_is_running(
    tmp_path: Path,
):
    powershell = _powershell_executable()
    previous_probe = _compile_health_probe(tmp_path)
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(previous_probe.read_bytes())
    previous_bytes = target.read_bytes()
    staged = tmp_path / ".SingRoute.update-v0.4.0.exe"
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", staged)
    backup = tmp_path / ".SingRoute.previous.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    health_path = tmp_path / UPDATE_HEALTH_FILENAME
    previous_started_path = tmp_path / ".SingRoute.previous-started"
    script = _write_installer_script()
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    running_target = subprocess.Popen([str(target)], env=environment)
    try:
        deadline = time.monotonic() + 10
        while not previous_started_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert previous_started_path.exists()

        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-SingRouteProcessId",
                "2147483647",
                "-StagedPath",
                str(staged),
                "-TargetPath",
                str(target),
                "-BackupPath",
                str(backup),
                "-ErrorPath",
                str(error_path),
                "-ReadyPath",
                str(ready_path),
                "-HealthPath",
                str(health_path),
                "-HealthToken",
                "f" * 64,
                "-ScriptPath",
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        assert result.returncode == 1, result.stderr
        assert running_target.poll() is None
        assert target.read_bytes() == previous_bytes
        assert staged.exists() is True
        assert backup.exists() is False
        assert ready_path.exists() is False
        error = error_path.read_text(encoding="utf-8")
        assert "already using" in error
        assert "previous version was not restarted" not in error
    finally:
        running_target.terminate()
        running_target.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_restores_backup_when_failed_target_disappears(
    tmp_path: Path,
    shell_change_events,
):
    powershell = _powershell_executable()
    target = tmp_path / "SingRoute.cmd"
    staged = tmp_path / ".SingRoute.update-v0.4.0.cmd"
    backup = tmp_path / ".SingRoute.previous.cmd"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    health_path = tmp_path / UPDATE_HEALTH_FILENAME
    previous_started_path = tmp_path / ".SingRoute.previous-started"
    previous_exited_path = tmp_path / ".SingRoute.previous-exited"
    target.write_text(
        "@echo off\n"
        '> "%~dp0.SingRoute.previous-started" <nul set /p "=ready"\n'
        "ping 127.0.0.1 -n 4 > nul\n"
        '> "%~dp0.SingRoute.previous-exited" <nul set /p "=done"\n',
        encoding="ascii",
    )
    previous_bytes = target.read_bytes()
    staged.write_text(
        '@echo off\ndel /f /q "%~f0"\nexit /b 23\n',
        encoding="ascii",
    )
    script = _write_installer_script()

    result = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SingRouteProcessId",
            "2147483647",
            "-StagedPath",
            str(staged),
            "-TargetPath",
            str(target),
            "-BackupPath",
            str(backup),
            "-ErrorPath",
            str(error_path),
            "-ReadyPath",
            str(ready_path),
            "-HealthPath",
            str(health_path),
            "-HealthToken",
            "e" * 64,
            "-ScriptPath",
            str(script),
            "-HealthTimeoutSeconds",
            "3",
            "-RollbackTimeoutSeconds",
            "5",
            "-RestartStabilitySeconds",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    deadline = time.monotonic() + 10
    while not previous_exited_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert result.returncode == 1, result.stderr
    assert target.read_bytes() == previous_bytes
    assert staged.exists() is False
    assert backup.exists() is False
    assert previous_started_path.read_text(encoding="ascii") == "ready"
    assert previous_exited_path.read_text(encoding="ascii") == "done"
    error = error_path.read_text(encoding="utf-8")
    assert "disappeared before rollback" in error
    assert any(path in (target, tmp_path) for path in shell_change_events())


def _release_info(version: str, digest: str) -> ReleaseInfo:
    tag = f"v{version}"
    prefix = f"https://github.com/oleg4bat/singroute/releases/download/{tag}"
    return ReleaseInfo(
        version=version,
        tag=tag,
        page_url=f"https://github.com/oleg4bat/singroute/releases/tag/{tag}",
        executable_url=f"{prefix}/{EXECUTABLE_ASSET_NAME}",
        executable_digest=digest,
    )


def _staged_update(tmp_path: Path, version: str = "0.4.0") -> StagedUpdate:
    executable = b"MZverified update"
    digest = hashlib.sha256(executable).hexdigest()
    target_path = tmp_path / EXECUTABLE_ASSET_NAME
    target_path.write_bytes(b"MZcurrent version")
    executable_path = tmp_path / f".SingRoute.update-v{version}.exe"
    executable_path.write_bytes(executable)
    return StagedUpdate(
        release=_release_info(version, digest),
        executable_path=executable_path,
        target_path=target_path,
        expected_digest=digest,
    )


def _compile_health_probe(tmp_path: Path) -> Path:
    source_path = tmp_path / "health-probe.cs"
    executable_path = tmp_path / "health-probe.exe"
    compiler_path = tmp_path / "compile-health-probe.ps1"
    source_path.write_text(
        r"""
using System;
using System.IO;
using System.Threading;

public static class Program
{
    [STAThread]
    public static void Main()
    {
        string applicationDirectory = AppDomain.CurrentDomain.BaseDirectory;
        string resetEnvironment = Environment.GetEnvironmentVariable(
            "PYINSTALLER_RESET_ENVIRONMENT"
        );
        if (resetEnvironment != "1")
        {
            File.WriteAllText(
                Path.Combine(applicationDirectory, ".SingRoute.reset-missing"),
                resetEnvironment ?? "missing"
            );
            return;
        }
        string healthPath = Environment.GetEnvironmentVariable(
            "SINGROUTE_UPDATE_HEALTH_PATH"
        );
        string healthToken = Environment.GetEnvironmentVariable(
            "SINGROUTE_UPDATE_HEALTH_TOKEN"
        );
        if (String.IsNullOrEmpty(healthPath) || String.IsNullOrEmpty(healthToken))
        {
            File.WriteAllText(
                Path.Combine(applicationDirectory, ".SingRoute.previous-started"),
                "ready"
            );
            Thread.Sleep(3000);
            File.WriteAllText(
                Path.Combine(applicationDirectory, ".SingRoute.previous-exited"),
                "done"
            );
            return;
        }
        File.WriteAllText(healthPath, healthToken);
        Thread.Sleep(4000);
        File.WriteAllText(healthPath + ".probe-exited", "done");
    }
}
""".strip(),
        encoding="utf-8",
    )
    compiler_path.write_text(
        r"""param(
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$source = [System.IO.File]::ReadAllText($SourcePath)
Add-Type `
    -TypeDefinition $source `
    -Language CSharp `
    -OutputAssembly $OutputPath `
    -OutputType WindowsApplication
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            str(_powershell_executable()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(compiler_path),
            "-SourcePath",
            str(source_path),
            "-OutputPath",
            str(executable_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert executable_path.is_file()
    return executable_path


def _release_json(tag: str, executable: bytes) -> bytes:
    version = tag.removeprefix("v")
    digest = hashlib.sha256(executable).hexdigest()
    release = _release_info(version, digest)
    return json.dumps(
        {
            "tag_name": tag,
            "html_url": release.page_url,
            "assets": [
                {
                    "name": EXECUTABLE_ASSET_NAME,
                    "browser_download_url": release.executable_url,
                    "digest": f"sha256:{digest}",
                },
            ],
        }
    ).encode("utf-8")
