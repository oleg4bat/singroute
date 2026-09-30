"""Verify that a packaged EXE contains every image from its source ICO."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pefile


def check_executable_icon(executable: Path, icon_path: Path) -> None:
    icon = icon_path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", icon)
    if (reserved, kind) != (0, 1) or count == 0:
        raise ValueError("Invalid source ICO")
    expected = []
    for index in range(count):
        size, offset = struct.unpack_from("<II", icon, 6 + 16 * index + 8)
        expected.append(icon[offset : offset + size])

    with pefile.PE(str(executable)) as pe:
        images = {}
        groups = []
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            for resource in entry.directory.entries:
                for language in resource.directory.entries:
                    data = language.data.struct
                    payload = pe.get_data(data.OffsetToData, data.Size)
                    if entry.id == pefile.RESOURCE_TYPE["RT_ICON"]:
                        images[resource.id] = payload
                    elif entry.id == pefile.RESOURCE_TYPE["RT_GROUP_ICON"]:
                        groups.append(payload)
        if not groups:
            raise ValueError("EXE has no icon group")
        group = groups[0]
        group_count = struct.unpack_from("<H", group, 4)[0]
        ids = [
            struct.unpack_from("<H", group, 6 + 14 * index + 12)[0]
            for index in range(group_count)
        ]
        if [images[image_id] for image_id in ids] != expected:
            raise ValueError("EXE icon differs from the source ICO")


if __name__ == "__main__":
    check_executable_icon(Path(sys.argv[1]), Path(sys.argv[2]))
    print("Packaged EXE icon matches the source ICO.")
