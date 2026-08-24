from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import Request

import pytest

from singroute.application.app_update import (
    AppUpdateError,
    CHECKSUM_ASSET_NAME,
    EXECUTABLE_ASSET_NAME,
    LATEST_RELEASE_API,
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
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.3.0")})

    result = check_for_update("0.2.0", opener=opener)

    assert result.update_available is True
    assert result.latest_release.version == "0.3.0"
    assert result.latest_release.tag == "v0.3.0"
    assert opener.requests[0].get_header("User-agent") == "SingRoute-Updater"


def test_check_for_update_does_not_downgrade():
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.2.0")})

    result = check_for_update("0.3.0", opener=opener)

    assert result.update_available is False


def test_check_rejects_release_assets_from_unexpected_location():
    payload = json.loads(_release_json("v0.3.0"))
    payload["assets"][0]["browser_download_url"] = "https://example.test/SingRoute.exe"
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="отсутствуют SingRoute.exe"):
        check_for_update("0.2.0", opener=opener)


def test_stage_update_verifies_sha256_and_keeps_verified_file(tmp_path: Path):
    executable = b"signed portable executable"
    digest = hashlib.sha256(executable).hexdigest()
    release = _release_info("0.3.0")
    opener = FakeOpener(
        {
            release.executable_url: executable,
            release.checksum_url: f"{digest}  SingRoute.exe\n".encode("ascii"),
        }
    )
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"old executable")

    staged = stage_update(release, target, opener=opener)

    assert staged.target_path == target.resolve()
    assert staged.executable_path.read_bytes() == executable
    assert target.read_bytes() == b"old executable"


def test_stage_update_removes_download_with_wrong_sha256(tmp_path: Path):
    release = _release_info("0.3.0")
    opener = FakeOpener(
        {
            release.executable_url: b"tampered",
            release.checksum_url: ("0" * 64 + "  SingRoute.exe\n").encode("ascii"),
        }
    )
    target = tmp_path / "SingRoute.exe"
    target.write_bytes(b"old executable")

    with pytest.raises(AppUpdateError, match="SHA-256"):
        stage_update(release, target, opener=opener)

    assert target.read_bytes() == b"old executable"
    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob(".*.update-*.exe")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater integration")
def test_installer_helper_replaces_target_after_process_exit(tmp_path: Path):
    source_executable = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    staged = tmp_path / ".SingRoute.update-v0.4.0.exe"
    target = tmp_path / "SingRoute.exe"
    shutil.copy2(source_executable, staged)
    target.write_bytes(b"old executable")
    expected_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    script = _write_installer_script()
    try:
        result = subprocess.run(
            [
                str(_powershell_executable()),
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
                "-ScriptPath",
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr
    assert staged.exists() is False
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_digest


def _release_info(version: str) -> ReleaseInfo:
    tag = f"v{version}"
    prefix = f"https://github.com/oleg4bat/singroute/releases/download/{tag}"
    return ReleaseInfo(
        version=version,
        tag=tag,
        page_url=f"https://github.com/oleg4bat/singroute/releases/tag/{tag}",
        executable_url=f"{prefix}/{EXECUTABLE_ASSET_NAME}",
        checksum_url=f"{prefix}/{CHECKSUM_ASSET_NAME}",
    )


def _release_json(tag: str) -> bytes:
    version = tag.removeprefix("v")
    release = _release_info(version)
    return json.dumps(
        {
            "tag_name": tag,
            "html_url": release.page_url,
            "assets": [
                {
                    "name": EXECUTABLE_ASSET_NAME,
                    "browser_download_url": release.executable_url,
                },
                {
                    "name": CHECKSUM_ASSET_NAME,
                    "browser_download_url": release.checksum_url,
                },
            ],
        }
    ).encode("utf-8")
