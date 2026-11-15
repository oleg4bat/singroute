"""Discover standard OpenWrt services and the config they actually load."""

from __future__ import annotations

import json
import posixpath
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace

from .router_client import RouterClient, run_commands
from .router_service import service_running_state
from .router_update import RouterUpdateError, _validate_config_path


@dataclass(frozen=True)
class RouterEngine:
    name: str
    binary_path: str
    config_path: str
    running: bool = False
    boot_enabled: bool = False
    issue: str = ""
    xray_enabled: str | None = None
    xray_asset_directory: str = "/usr/share/xray"
    service_state_issue: str = ""
    service_exists: bool = True

    @property
    def service_name(self) -> str:
        return self.name


@dataclass(frozen=True)
class RouterInfo:
    openwrt_release: str
    engines: tuple[RouterEngine, ...] = ()

    @property
    def available_engines(self) -> tuple[RouterEngine, ...]:
        return self.engines

    @property
    def preferred_engine(self) -> str | None:
        return next(
            (e.name for e in self.engines if e.running),
            self.engines[0].name if self.engines else None,
        )


class RouterCompatibilityError(RuntimeError):
    """The connected host cannot run the update workflow."""


def _command_config_path(name: str, command: object) -> str:
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) for arg in command)
        or posixpath.basename(command[0]) != name
    ):
        raise ValueError("Unsupported service command")
    files = []
    flags = {"-c", "--config"} if name == "sing-box" else {"-c", "-config", "--config"}
    directories = {"-C", "--config-directory", "-confdir", "--confdir"}
    index = 1
    while index < len(command):
        arg, equals, value = command[index].partition("=")
        if arg in directories:
            raise ValueError("Config directories are ambiguous")
        if arg in flags or (name == "xray" and arg in {"-format", "--format"}):
            if not equals:
                index += 1
                if index == len(command):
                    raise ValueError("Missing option value")
                value = command[index]
            if arg in flags:
                files.append(value)
            elif value != "json":
                raise ValueError("Unsupported config format")
        index += 1
    if len(files) != 1:
        raise ValueError("A single config is required")
    _validate_config_path(files[0])
    return files[0]


def _procd_config_path(text: str, name: str) -> str:
    data = json.loads(text)
    service = data.get(name) if isinstance(data, dict) else None
    instances = service.get("instances") if isinstance(service, dict) else None
    if not isinstance(instances, dict):
        raise ValueError("Missing instances")
    running = [
        instance
        for instance in instances.values()
        if isinstance(instance, dict) and instance.get("running") is True
    ]
    if len(running) != 1:
        raise ValueError("A single running instance is required")
    return _command_config_path(name, running[0].get("command"))


def _running_config_path(client: RouterClient, name: str) -> str:
    query = shlex.quote(json.dumps({"name": name}, separators=(",", ":")))
    result = client.run(f"ubus call service list {query}")
    if result.exit_code != 0:
        raise ValueError("Service state unavailable")
    return _procd_config_path(result.stdout, name)


def _xray_options(text: str) -> dict[str, list[str]]:
    options = {}
    relevant = {
        "xray.config.conffiles",
        "xray.config.confdir",
        "xray.enabled.enabled",
        "xray.config.format",
        "xray.config.datadir",
    }
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in relevant:
            options[key] = shlex.split(value)
    return options


def refresh_service_states(
    client: RouterClient, info: RouterInfo, *, include_boot: bool = False
) -> RouterInfo:
    """Refresh only the mutable state needed for apply or service rollback."""
    commands = {}
    for index, engine in enumerate(info.engines):
        if not engine.service_exists:
            continue
        service = shlex.quote("/etc/init.d/" + engine.name)
        commands[f"status_{index}"] = f"{service} status"
        if include_boot:
            commands[f"boot_{index}"] = f"{service} enabled"
    results = run_commands(client, commands, "Проверяю состояние служб")
    engines = []
    for index, engine in enumerate(info.engines):
        if not engine.service_exists:
            engines.append(engine)
            continue
        state = service_running_state(results[f"status_{index}"])
        boot = results.get(f"boot_{index}")
        issue = (
            ""
            if state is not None and (boot is None or boot.exit_code in {0, 1})
            else "Не удалось определить состояние службы. Переподключитесь к роутеру."
        )
        engines.append(
            replace(
                engine,
                running=state is True,
                boot_enabled=boot.exit_code == 0
                if boot is not None
                else engine.boot_enabled,
                service_state_issue=issue,
            )
        )
    return replace(info, engines=tuple(engines))


def inspect_router(
    client: RouterClient, report: Callable[[str], None] | None = None
) -> RouterInfo:
    """Discover files from procd/UCI; never guess a path or accept overrides."""
    notify = report or (lambda _: None)
    notify("Проверяю, что устройство работает на OpenWrt…")
    commands = {"release": "cat /etc/openwrt_release"}
    for name in ("sing-box", "xray"):
        service = f"/etc/init.d/{name}"
        commands.update(
            {
                f"{name}_service": f"test -x {service}",
                f"{name}_binary": f"command -v {name}",
                f"{name}_status": f"{service} status",
                f"{name}_boot": f"{service} enabled",
                f"{name}_config": "uci -q get sing-box.main.conffile"
                if name == "sing-box"
                else "uci -q show xray",
                f"{name}_procd": "ubus call service list "
                + shlex.quote(json.dumps({"name": name}, separators=(",", ":"))),
            }
        )
    results = run_commands(client, commands, "Определяю службы и конфиги OpenWrt")
    release = results["release"]
    if release.exit_code != 0 or "OpenWrt" not in release.stdout:
        raise RouterCompatibilityError(
            "Удалённое устройство не распознано как OpenWrt."
        )
    notify("Проверяю службы sing-box и Xray…")
    engines = []
    for name in ("sing-box", "xray"):
        service = f"/etc/init.d/{name}"
        service_exists = results[f"{name}_service"].exit_code == 0
        binary = results[f"{name}_binary"]
        if binary.exit_code in {1, 127} or (
            binary.exit_code == 0 and not binary.stdout.strip()
        ):
            if not service_exists:
                continue
            binary_path = ""
        elif binary.exit_code != 0:
            raise RouterCompatibilityError("Не удалось проверить движки на роутере.")
        else:
            binary_path = binary.stdout.strip()
        issue = state_issue = ""
        running = boot = False
        if service_exists:
            status = results[f"{name}_status"]
            enabled = results[f"{name}_boot"]
            state = service_running_state(status)
            running, boot = state is True, enabled.exit_code == 0
            if state is None or enabled.exit_code not in {0, 1}:
                state_issue = "Не удалось определить состояние службы или автозапуска. Конфиг можно просмотреть; применение пока недоступно."
        else:
            issue = f"Служба {service} не найдена. Установите и настройте {name} на роутере."
        if not binary_path:
            issue = f"Служба {name} найдена, но движок не установлен. Переустановите пакет на роутере."
        path = ""
        xray_enabled = None
        assets = "/usr/share/xray"
        if service_exists:
            if name == "sing-box":
                selected = results[f"{name}_config"]
                if selected.exit_code == 0:
                    path = selected.stdout.strip()
                else:
                    issue = (
                        issue
                        or "Не удалось определить конфиг sing-box. Настройте файл в службе OpenWrt."
                    )
            else:
                selected = results[f"{name}_config"]
                try:
                    options = (
                        _xray_options(selected.stdout)
                        if selected.exit_code == 0
                        else {}
                    )
                except ValueError:
                    options = {}
                files = options.get("xray.config.conffiles", [])
                directory = options.get("xray.config.confdir", [])
                if (
                    len(files) == 1
                    and len(files[0].split()) == 1
                    and not any(directory)
                ):
                    path = files[0]
                else:
                    issue = (
                        issue
                        or "Не удалось однозначно определить конфиг Xray. Настройте службу OpenWrt на один JSON-файл без каталога конфигов."
                    )
                flag = options.get("xray.enabled.enabled", [])
                xray_enabled = flag[0] if len(flag) == 1 else None
                if xray_enabled not in {"0", "1"}:
                    issue = (
                        issue
                        or "Не удалось прочитать настройки включения Xray в OpenWrt."
                    )
                if options.get("xray.config.format", []) not in ([], [""], ["json"]):
                    issue = issue or "Служба Xray должна использовать формат JSON."
                asset_directory = options.get("xray.config.datadir", [])
                if len(asset_directory) > 1:
                    issue = issue or "Не удалось прочитать каталог ресурсов Xray."
                assets = (
                    asset_directory[0]
                    if asset_directory and asset_directory[0]
                    else assets
                )
            if running:
                try:
                    procd = results[f"{name}_procd"]
                    if procd.exit_code != 0:
                        raise ValueError("Service state unavailable")
                    active_path = _procd_config_path(procd.stdout, name)
                    if path and active_path != path:
                        issue = (
                            issue
                            or f"Настройки службы {name} отличаются от запущенного конфига. Перезапустите службу на роутере и переподключитесь."
                        )
                    path = active_path
                except (ValueError, RouterUpdateError):
                    path = ""
                    issue = (
                        issue
                        or f"Не удалось однозначно определить конфиг работающей службы {name}. Проверьте настройки службы OpenWrt."
                    )
            try:
                _validate_config_path(path)
            except RouterUpdateError:
                path = ""
                issue = (
                    issue
                    or f"Служба {name} не указала корректный абсолютный путь конфига. Настройте её в OpenWrt."
                )
        engines.append(
            RouterEngine(
                name=name,
                binary_path=binary_path,
                config_path=path,
                running=running,
                boot_enabled=boot,
                issue=issue,
                xray_enabled=xray_enabled,
                xray_asset_directory=assets,
                service_state_issue=state_issue,
                service_exists=service_exists,
            )
        )
    notify("Состояние движков определено.")
    return RouterInfo(_openwrt_description(release.stdout), tuple(engines))


def _openwrt_description(release_text: str) -> str:
    for line in release_text.splitlines():
        if line.startswith("DISTRIB_DESCRIPTION="):
            return line.partition("=")[2].strip().strip("'\"")
    return "OpenWrt"
