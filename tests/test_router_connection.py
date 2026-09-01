from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from singroute.application.router_client import CommandResult
from singroute.application.router_connection import (
    RouterCompatibilityError,
    inspect_router,
)


def test_inspect_router_returns_release_and_binary_and_reports_progress():
    client = FakeRouterClient()
    progress: list[str] = []

    result = inspect_router(
        client,
        "/etc/sing-box/config.json",
        "sing-box",
        progress.append,
    )

    assert result.openwrt_release == "OpenWrt 24.10"
    assert result.sing_box_path == "/usr/bin/sing-box"
    assert len(progress) == 3
    assert client.commands == [
        "cat /etc/openwrt_release",
        "command -v sing-box",
        "test -r /etc/sing-box/config.json",
        "test -x /etc/init.d/sing-box",
    ]


@pytest.mark.parametrize(
    ("failed_command", "message"),
    [
        ("cat /etc/openwrt_release", "OpenWrt"),
        ("command -v sing-box", "sing-box"),
        ("test -r /etc/sing-box/config.json", "Конфиг"),
        ("test -x /etc/init.d/sing-box", "Служба"),
    ],
)
def test_inspect_router_stops_on_failed_compatibility_check(
    failed_command: str,
    message: str,
):
    client = FakeRouterClient(failed_command=failed_command)

    with pytest.raises(RouterCompatibilityError, match=message):
        inspect_router(client, "/etc/sing-box/config.json", "sing-box")


@dataclass
class FakeRouterClient:
    failed_command: str | None = None
    commands: list[str] = field(default_factory=list)

    def run(self, command: str) -> CommandResult:
        self.commands.append(command)
        if command == self.failed_command:
            return CommandResult(command, 1)
        stdout = {
            "cat /etc/openwrt_release": "DISTRIB_DESCRIPTION='OpenWrt 24.10'\n",
            "command -v sing-box": "/usr/bin/sing-box\n",
        }.get(command, "")
        return CommandResult(command, 0, stdout=stdout)

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def write_text(self, path: str, content: str) -> None:
        raise NotImplementedError
