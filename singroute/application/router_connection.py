"""Checks performed once after an SSH connection is established."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass

from singroute.application.router_client import RouterClient
from singroute.application.router_service import service_running_state


@dataclass(frozen=True)
class RouterEngine:
    name: str
    binary_path: str
    config_path: str
    service_name: str
    running: bool = False
    boot_enabled: bool = False
    issue: str = ""
    xray_enabled: str | None = None
    xray_config_text: str | None = None
    xray_asset_directory: str = "/usr/share/xray"
    service_state_issue: str = ""
    xray_needs_single_config: bool = False


@dataclass(frozen=True)
class RouterInfo:
    openwrt_release: str
    sing_box_path: str = ""
    engines: tuple[RouterEngine, ...] = ()

    @property
    def available_engines(self) -> tuple[RouterEngine, ...]:
        # Compatibility with callers that construct RouterInfo directly.
        return self.engines or (
            (
                RouterEngine(
                    "sing-box",
                    self.sing_box_path,
                    "/etc/sing-box/config.json",
                    "sing-box",
                    True,
                ),
            )
            if self.sing_box_path
            else ()
        )

    @property
    def preferred_engine(self) -> str | None:
        engines = self.available_engines
        return next(
            (e.name for e in engines if e.running), engines[0].name if engines else None
        )


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

    notify("Проверяю наличие sing-box и Xray…")
    engines = []
    for name, path, service in (
        ("sing-box", config_path, service_name),
        ("xray", "/etc/xray/config.json", "xray"),
    ):
        binary = client.run(f"command -v {name}")
        if binary.exit_code == 1 or (
            binary.exit_code == 0 and not binary.stdout.strip()
        ):
            continue
        if binary.exit_code != 0:
            raise RouterCompatibilityError(
                "Не удалось подтвердить наличие движка на роутере."
            )
        issue = ""
        xray_enabled = xray_config_text = None
        asset_directory = "/usr/share/xray"
        xray_needs_single_config = False
        if name == "xray":
            config_exists = client.run("test -r /etc/config/xray")
            if config_exists.exit_code == 0:
                xray_config_text = client.read_text("/etc/config/xray")
                files = client.run("uci -q get xray.config.conffiles")
                directory = client.run("uci -q get xray.config.confdir")
                flag = client.run("uci -q get xray.enabled.enabled")
                format_result = client.run("uci -q get xray.config.format")
                assets = client.run("uci -q get xray.config.datadir")
                if any(
                    r.exit_code not in {0, 1}
                    for r in (files, directory, flag, format_result, assets)
                ):
                    raise RouterCompatibilityError(
                        "Не удалось прочитать настройки службы Xray."
                    )
                # SingRoute always edits its primary file, never package examples
                # or fragments found in confdir. Reconcile the service on apply.
                xray_needs_single_config = files.stdout.split() != [path] or bool(
                    directory.stdout.strip()
                )
                xray_enabled = flag.stdout.strip() if flag.exit_code == 0 else None
                if xray_enabled not in {"0", "1"}:
                    issue = "В /etc/config/xray задайте enabled.enabled: 0 или 1."
                if format_result.stdout.strip() not in {"", "json"}:
                    issue = "Для импорта Xray служба должна использовать формат JSON."
                asset_directory = assets.stdout.strip() or "/usr/share/xray"
            else:
                issue = "Не найден /etc/config/xray. Настройте службу пакета xray-core."
        service_command = shlex.quote(f"/etc/init.d/{service}")
        if client.run(f"test -r {shlex.quote(path)}").exit_code != 0:
            issue = f"Конфиг {path} не найден или недоступен для чтения."
        service_exists = client.run(f"test -x {service_command}").exit_code == 0
        if not service_exists:
            issue = f"Служба /etc/init.d/{service} не найдена."
        running = boot = False
        service_state_issue = ""
        if service_exists:
            status = client.run(f"{service_command} status")
            enabled = client.run(f"{service_command} enabled")
            state = service_running_state(status)
            if state is None or enabled.exit_code not in {0, 1}:
                service_state_issue = "Не удалось определить состояние службы или автозапуска. Конфиг можно просмотреть; применение пока недоступно."
            running, boot = state is True, enabled.exit_code == 0
        engines.append(
            RouterEngine(
                name,
                binary.stdout.strip(),
                path,
                service,
                running,
                boot,
                issue,
                xray_enabled,
                xray_config_text,
                asset_directory,
                service_state_issue,
                xray_needs_single_config,
            )
        )
    notify("Состояние движков определено.")
    return RouterInfo(
        openwrt_release=_openwrt_description(release.stdout),
        sing_box_path=next(
            (e.binary_path for e in engines if e.name == "sing-box"), ""
        ),
        engines=tuple(engines),
    )


def _openwrt_description(release_text: str) -> str:
    for line in release_text.splitlines():
        if line.startswith("DISTRIB_DESCRIPTION="):
            return line.partition("=")[2].strip().strip("'\"")
    return "OpenWrt"
