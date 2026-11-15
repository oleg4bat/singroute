"""Router I/O boundary shared by application use cases and SSH infrastructure."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class RouterFileReadError(RuntimeError):
    """The remote command completed, but the requested file could not be read."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"Не удалось прочитать {path}. Проверьте наличие файла и права доступа."
        )


class RouterClient(Protocol):
    def read_text(self, path: str) -> str:
        """Read text from a router file."""

    def write_text(self, path: str, content: str) -> None:
        """Write text to a router file."""

    def run(self, command: str) -> CommandResult:
        """Run a router command and return its process result."""


def run_commands(
    client: RouterClient, commands: dict[str, str], description: str
) -> dict[str, CommandResult]:
    """Collect independent command results over one SSH channel."""
    if not commands:
        return {}
    marker = "singroute_" + secrets.token_hex(8)
    lines = ["# " + description]
    for index, command in enumerate(commands.values()):
        lines.extend(
            [
                f"printf '\\n{marker}:{index}\\n'",
                "{\n" + command + "\n} 2>&1",
                "singroute_status=$?",
                f"printf '\\n{marker}:end:%s\\n' \"$singroute_status\"",
            ]
        )
    result = client.run("\n".join(lines))
    records = re.findall(
        rf"\n{marker}:(\d+)\n(.*?)\n{marker}:end:(\d+)\n",
        result.stdout,
        re.DOTALL,
    )
    if result.exit_code not in {0, -1} or [int(row[0]) for row in records] != list(
        range(len(commands))
    ):
        raise RuntimeError(
            "Не удалось получить состояние роутера. Переподключитесь и повторите операцию."
        )
    return {
        key: CommandResult(command, int(row[2]), row[1])
        for (key, command), row in zip(commands.items(), records, strict=True)
    }
