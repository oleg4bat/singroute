"""Router I/O boundary shared by application use cases and SSH infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class RouterClient(Protocol):
    def read_text(self, path: str) -> str:
        """Read text from a router file."""

    def write_text(self, path: str, content: str) -> None:
        """Write text to a router file."""

    def run(self, command: str) -> CommandResult:
        """Run a router command and return its process result."""
