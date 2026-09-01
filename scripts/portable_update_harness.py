"""Frozen executable used by the portable updater black-box test."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import BinaryIO, Self

from singroute.application.app_update import (
    ReleaseInfo,
    launch_staged_update,
    stage_update,
    take_update_error,
)

PLAN_FILENAME = ".SingRoute.e2e-plan.json"
DOWNLOAD_MARKER_FILENAME = ".SingRoute.e2e-downloaded"
HELPER_MARKER_FILENAME = ".SingRoute.e2e-helper-ready"
ROLLBACK_MARKER_FILENAME = ".SingRoute.e2e-rollback-restarted"


class LocalDownload:
    """Expose a local payload through the response interface used by stage_update."""

    def __init__(self, path: Path) -> None:
        self._stream: BinaryIO = path.open("rb")

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self._stream.close()


def main() -> int:
    target_path = Path(sys.executable).resolve()
    application_directory = target_path.parent
    rollback_error = take_update_error(target_path)
    if rollback_error is not None:
        rollback_marker = application_directory / ROLLBACK_MARKER_FILENAME
        rollback_marker.write_text(rollback_error, encoding="utf-8")
        # The real helper verifies that the restored process remains alive.
        time.sleep(10)
        return 0

    plan_path = application_directory / PLAN_FILENAME
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    payload_path = Path(plan["payload_path"]).resolve(strict=True)
    version = str(plan["version"])
    expected_digest = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    tag = f"v{version}"
    release = ReleaseInfo(
        version=version,
        tag=tag,
        page_url=f"https://github.com/oleg4bat/singroute/releases/tag/{tag}",
        executable_url=(
            "https://github.com/oleg4bat/singroute/releases/download/"
            f"{tag}/SingRoute.exe"
        ),
        executable_digest=expected_digest,
    )

    def open_local_payload(request: object, *, timeout: int) -> LocalDownload:
        del request
        assert timeout > 0
        return LocalDownload(payload_path)

    staged = stage_update(release, target_path, opener=open_local_payload)
    download_marker = application_directory / DOWNLOAD_MARKER_FILENAME
    download_marker.write_text(expected_digest, encoding="ascii")
    launch_staged_update(staged)
    helper_marker = application_directory / HELPER_MARKER_FILENAME
    helper_marker.write_text("ready", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
