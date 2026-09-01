"""Checks performed once after an SSH connection is established."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass

from singroute.application.router_client import RouterClient


@dataclass(frozen=True)
class RouterInfo:
    openwrt_release: str
    sing_box_path: str


class RouterCompatibilityError(RuntimeError):
    """Raised when the connected host cannot run the update workflow."""


def inspect_router(
    client: RouterClient,
    config_path: str,
    service_name: str,
    report: Callable[[str], None] | None = None,
) -> RouterInfo:
    """Verify OpenWrt, sing-box, config, and init service availability."""
    notify = report or (lambda _: None)

    notify("Проверяю, что устройство работает на OpenWrt…")
    release = client.run("cat /etc/openwrt_release")
    if release.exit_code != 0 or "OpenWrt" not in release.stdout:
        raise RouterCompatibilityError(
            "Удалённое устройство не распознано как OpenWrt."
        )

    notify("Проверяю наличие sing-box…")
    binary = client.run("command -v sing-box")
    if binary.exit_code != 0 or not binary.stdout.strip():
        raise RouterCompatibilityError(
            "На роутере не найден исполняемый файл sing-box."
        )

    notify("Проверяю доступ к конфигу и службе…")
    config_check = client.run(f"test -r {shlex.quote(config_path)}")
    if config_check.exit_code != 0:
        raise RouterCompatibilityError(
            f"Конфиг {config_path} не найден или недоступен для чтения."
        )

    service_path = f"/etc/init.d/{service_name}"
    service_check = client.run(f"test -x {shlex.quote(service_path)}")
    if service_check.exit_code != 0:
        raise RouterCompatibilityError(f"Служба {service_path} не найдена.")

    return RouterInfo(
        openwrt_release=_openwrt_description(release.stdout),
        sing_box_path=binary.stdout.strip(),
    )


def _openwrt_description(release_text: str) -> str:
    for line in release_text.splitlines():
        if line.startswith("DISTRIB_DESCRIPTION="):
            return line.partition("=")[2].strip().strip("'\"")
    return "OpenWrt"
