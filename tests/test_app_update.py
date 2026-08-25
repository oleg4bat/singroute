from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import Request

import pytest

from singroute.application.app_update import (
    AppUpdateError,
    CHECKSUM_ASSET_NAME,
    EXECUTABLE_ASSET_NAME,
    LATEST_RELEASE_API,
    MAX_METADATA_BYTES,
    ReleaseInfo,
    _powershell_executable,
    _write_installer_script,
    check_for_update,
    stage_update,
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
    opener = FakeOpener(
        {LATEST_RELEASE_API: _release_json("v0.3.3", executable)}
    )

    result = check_for_update("0.3.2", opener=opener)

    assert result.update_available is True
    assert result.latest_release.version == "0.3.3"
    assert result.latest_release.tag == "v0.3.3"
    assert result.latest_release.executable_digest == hashlib.sha256(
        executable
    ).hexdigest()
    assert opener.requests[0].get_header("User-agent") == "SingRoute-Updater"


def test_check_for_update_does_not_downgrade():
    opener = FakeOpener(
        {LATEST_RELEASE_API: _release_json("v0.3.2", b"MZrelease")}
    )

    result = check_for_update("0.3.3", opener=opener)

    assert result.update_available is False


def test_check_rejects_release_assets_from_unexpected_location():
    payload = json.loads(_release_json("v0.3.3", b"MZrelease"))
    payload["assets"][0]["browser_download_url"] = (
        "https://example.test/SingRoute.exe"
    )
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="отсутствуют SingRoute.exe"):
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


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_stage_update_verifies_all_digests_and_keeps_verified_file(
    tmp_path: Path,
    line_ending: bytes,
):
    executable = b"MZsigned portable executable"
    digest = hashlib.sha256(executable).hexdigest()
    release = _release_info("0.3.3", digest)
    opener = FakeOpener(
        {
            release.executable_url: executable,
            release.checksum_url: (
                f"{digest}  SingRoute.exe".encode("ascii") + line_ending
            ),
        }
    )
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZold executable")

    staged = stage_update(release, target, opener=opener)

    assert staged.target_path == target.resolve()
    assert staged.executable_path.read_bytes() == executable
    assert staged.expected_digest == digest
    assert target.read_bytes() == b"MZold executable"


def test_stage_update_rejects_checksum_that_disagrees_with_github(tmp_path: Path):
    executable = b"MZsigned portable executable"
    digest = hashlib.sha256(executable).hexdigest()
    release = _release_info("0.3.3", digest)
    opener = FakeOpener(
        {
            release.checksum_url: (
                "0" * 64 + "  SingRoute.exe\n"
            ).encode("ascii"),
        }
    )
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZold executable")

    with pytest.raises(AppUpdateError, match="digest"):
        stage_update(release, target, opener=opener)


def test_stage_update_removes_download_with_wrong_digest(tmp_path: Path):
    expected = b"MZexpected"
    digest = hashlib.sha256(expected).hexdigest()
    release = _release_info("0.3.3", digest)
    opener = FakeOpener(
        {
            release.executable_url: b"MZtampered",
            release.checksum_url: f"{digest}  SingRoute.exe\n".encode("ascii"),
        }
    )
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"MZold executable")

    with pytest.raises(AppUpdateError, match="целостности"):
        stage_update(release, target, opener=opener)

    assert target.read_bytes() == b"MZold executable"
    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob(".*.update-*.exe")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_retries_until_locked_target_can_be_replaced(tmp_path: Path):
    powershell = _powershell_executable()
    source_executable = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    staged = tmp_path / ".SingRoute.update-v0.3.3.exe"
    target = tmp_path / "SingRoute.exe"
    backup = tmp_path / ".SingRoute.previous.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    shutil.copy2(source_executable, staged)
    target.write_bytes(b"MZold executable")
    expected_digest = hashlib.sha256(staged.read_bytes()).hexdigest()

    ready_path = tmp_path / "locked.ready"
    locker_script = tmp_path / "hold-lock.ps1"
    locker_script.write_text(
        r'''param([string]$TargetPath, [string]$ReadyPath)
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
''',
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
            str(ready_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready_path.exists(), locker.stderr.read() if locker.stderr else ""

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
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_digest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_launched_installer_survives_parent_process_exit(tmp_path: Path):
    source_executable = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    staged = tmp_path / ".SingRoute.update-v0.3.5.exe"
    target = tmp_path / "SingRoute.exe"
    backup = tmp_path / ".SingRoute.previous.exe"
    error_path = tmp_path / "SingRoute-update-error.txt"
    ready_path = tmp_path / ".SingRoute.update-ready"
    shutil.copy2(source_executable, staged)
    target.write_bytes(b"MZold executable")
    expected_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    launcher_code = r'''
from pathlib import Path
import sys

from singroute.application.app_update import (
    ReleaseInfo,
    StagedUpdate,
    launch_staged_update,
)

target = Path(sys.argv[1])
staged = Path(sys.argv[2])
digest = sys.argv[3]
release = ReleaseInfo(
    version="0.3.5",
    tag="v0.3.5",
    page_url="https://github.com/oleg4bat/singroute/releases/tag/v0.3.5",
    executable_url="https://example.test/SingRoute.exe",
    checksum_url="https://example.test/SingRoute.exe.sha256",
    executable_digest=digest,
)
launch_staged_update(StagedUpdate(release, staged, target, digest))
'''

    launcher = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher_code,
            str(target),
            str(staged),
            expected_digest,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not staged.exists() and not backup.exists() and not ready_path.exists():
            break
        time.sleep(0.05)

    assert launcher.returncode == 0, launcher.stderr
    assert staged.exists() is False
    assert backup.exists() is False
    assert error_path.exists() is False
    assert ready_path.exists() is False
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_digest


def _release_info(version: str, digest: str) -> ReleaseInfo:
    tag = f"v{version}"
    prefix = f"https://github.com/oleg4bat/singroute/releases/download/{tag}"
    return ReleaseInfo(
        version=version,
        tag=tag,
        page_url=f"https://github.com/oleg4bat/singroute/releases/tag/{tag}",
        executable_url=f"{prefix}/{EXECUTABLE_ASSET_NAME}",
        checksum_url=f"{prefix}/{CHECKSUM_ASSET_NAME}",
        executable_digest=digest,
    )


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
                {
                    "name": CHECKSUM_ASSET_NAME,
                    "browser_download_url": release.checksum_url,
                    "digest": f"sha256:{'0' * 64}",
                },
            ],
        }
    ).encode("utf-8")
