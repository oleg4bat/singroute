from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.create_release_checksum import (
    LEGACY_030_CHECKSUM_PATTERN,
    build_checksum_bytes,
    write_checksum,
)


def test_generated_checksum_is_compatible_with_030_updater(tmp_path: Path):
    asset = tmp_path / "SingRoute.exe"
    asset.write_bytes(b"portable executable")

    checksum = build_checksum_bytes(asset)

    expected_digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    assert checksum == f"{expected_digest}  SingRoute.exe\n".encode("ascii")
    assert b"\r" not in checksum
    assert LEGACY_030_CHECKSUM_PATTERN.search(checksum.decode("ascii")) is not None


def test_windows_crlf_reproduces_030_checksum_format_failure(tmp_path: Path):
    asset = tmp_path / "SingRoute.exe"
    asset.write_bytes(b"portable executable")
    checksum_with_crlf = build_checksum_bytes(asset).replace(b"\n", b"\r\n")

    assert (
        LEGACY_030_CHECKSUM_PATTERN.search(checksum_with_crlf.decode("ascii"))
        is None
    )


def test_release_workflow_uses_tested_checksum_generator(tmp_path: Path):
    asset = tmp_path / "SingRoute.exe"
    asset.write_bytes(b"portable executable")

    output_path = write_checksum(asset)

    assert output_path == tmp_path / "SingRoute.exe.sha256"
    assert output_path.read_bytes() == build_checksum_bytes(asset)
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "python scripts/create_release_checksum.py dist/SingRoute.exe" in workflow
    assert "Set-Content" not in workflow
