"""Create a checksum file compatible with the SingRoute 0.3.0 updater."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re


EXECUTABLE_NAME = "SingRoute.exe"
LEGACY_030_CHECKSUM_PATTERN = re.compile(
    rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(EXECUTABLE_NAME)}$",
    re.MULTILINE,
)


def build_checksum_bytes(asset_path: Path) -> bytes:
    """Return an ASCII checksum line with an explicit Unix line ending."""
    if asset_path.name != EXECUTABLE_NAME:
        raise ValueError(f"Expected release asset named {EXECUTABLE_NAME}.")
    digest = hashlib.sha256()
    with asset_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    content = f"{digest.hexdigest()}  {EXECUTABLE_NAME}\n".encode("ascii")
    if LEGACY_030_CHECKSUM_PATTERN.search(content.decode("ascii")) is None:
        raise ValueError("Generated checksum is incompatible with SingRoute 0.3.0.")
    return content


def write_checksum(asset_path: Path) -> Path:
    """Write `<asset>.sha256` without platform-dependent newline conversion."""
    output_path = asset_path.with_suffix(asset_path.suffix + ".sha256")
    output_path.write_bytes(build_checksum_bytes(asset_path))
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", type=Path)
    arguments = parser.parse_args()
    output_path = write_checksum(arguments.asset)
    print(output_path)


if __name__ == "__main__":
    main()
