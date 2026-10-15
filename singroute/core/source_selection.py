"""Select connection data from a source without importing its routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unicodedata import category, normalize
from uuid import UUID

from .converters import normalize_outbound_to_singbox
from .errors import ConfigPatchError, SourceGroupError, UnsupportedVlessTransportError
from .patcher import SERVICE_OUTBOUND_TYPES, select_exported_outbound

# Group nodes must be self-contained connections our converters can validate.
GROUP_PROXY_TYPES = {"vless", "trojan", "hysteria2"}


@dataclass(frozen=True)
class ProxySelection:
    servers: tuple[dict[str, Any], ...]
    automatic: bool = False
    warnings: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


def select_source(config: dict[str, Any]) -> ProxySelection:
    """Use the primary source group when identifiable, otherwise the single path."""
    if not isinstance(config, dict):
        raise ConfigPatchError("Исходный конфиг должен быть объектом JSON.")
    routing = config.get("routing", {})
    if not isinstance(routing, dict):
        raise ConfigPatchError("Некорректная маршрутизация исходного конфига.")
    balancers = routing.get("balancers", [])
    if balancers:
        candidates = _xray_candidates(config, routing, balancers)
        return _convert_group(candidates)

    outbounds = config.get("outbounds")
    if isinstance(outbounds, list) and outbounds:
        route = config.get("route", {})
        final = route.get("final") if isinstance(route, dict) else None
        active = outbounds[0]
        if final:
            active = next(
                (
                    item
                    for item in outbounds
                    if isinstance(item, dict) and item.get("tag") == final
                ),
                None,
            )
            if active is None:
                raise SourceGroupError("Исходный выход по умолчанию отсутствует.")
        active_type = active.get("type") if isinstance(active, dict) else None
        if isinstance(active_type, str) and active_type in {"urltest", "selector"}:
            tags = _strings(active.get("outbounds"))
            candidates = _tagged_outbounds(outbounds)
            if any(tag not in candidates for tag in tags):
                raise SourceGroupError(
                    "В исходной группе отсутствует описание сервера."
                )
            return _convert_group([candidates[tag] for tag in tags])
    return ProxySelection((select_exported_outbound(config),))


def _xray_candidates(
    config: dict[str, Any], routing: dict[str, Any], balancers: Any
) -> list[dict[str, Any]]:
    primary = _xray_primary_balancer(config, routing, balancers)
    outbounds = _tagged_outbounds(config.get("outbounds"))
    prefixes = _strings(primary.get("selector"))
    return [
        item
        for tag, item in outbounds.items()
        if any(tag.startswith(p) for p in prefixes)
    ]


def _xray_primary_balancer(
    config: dict[str, Any], routing: dict[str, Any], balancers: Any
) -> dict[str, Any]:
    if not isinstance(balancers, list) or not all(
        isinstance(b, dict) for b in balancers
    ):
        raise SourceGroupError("Некорректный список групп серверов.")
    outbounds = _tagged_outbounds(config.get("outbounds"))
    rules = routing.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise SourceGroupError("Некорректные правила выбора группы серверов.")

    # Loopback inbounds are internal fallback entry points, not primary clients.
    fallback_inbounds = {
        item["settings"]["inboundTag"]
        for item in outbounds.values()
        if item.get("protocol") == "loopback"
        and isinstance(item.get("settings"), dict)
        and isinstance(item["settings"].get("inboundTag"), str)
    }
    primary_tags = set()
    for rule in rules:
        if not rule.get("balancerTag") or rule.get("outboundTag"):
            continue
        inbound_tags = rule.get("inboundTag", [])
        if not isinstance(inbound_tags, list) or any(
            not isinstance(t, str) for t in inbound_tags
        ):
            raise SourceGroupError("Некорректные входы исходной группы.")
        if inbound_tags and all(tag in fallback_inbounds for tag in inbound_tags):
            continue
        # Domain/port-specific groups cannot define one unambiguous default exit.
        if set(rule) - {"type", "network", "inboundTag", "balancerTag", "ruleTag"}:
            continue
        tag = rule["balancerTag"]
        if not isinstance(tag, str):
            raise SourceGroupError("Некорректная ссылка на группу серверов.")
        primary_tags.add(tag)
    if len(primary_tags) != 1:
        raise SourceGroupError(
            "Не удалось однозначно определить основную группу серверов."
        )
    matches = [
        b
        for b in balancers
        if isinstance(b.get("tag"), str) and b["tag"] in primary_tags
    ]
    if len(matches) != 1:
        raise SourceGroupError("Основная группа серверов отсутствует или неоднозначна.")
    return matches[0]


def _strings(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(v, str) or not v for v in value)
    ):
        raise SourceGroupError("В группе должен быть непустой список серверов.")
    if len(value) != len(set(value)):
        raise SourceGroupError("В группе повторяются ссылки на серверы.")
    return value


def _tagged_outbounds(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SourceGroupError("В исходном конфиге отсутствуют серверы.")
    result = {}
    for item in value:
        if not isinstance(item, dict):
            raise SourceGroupError("Некорректное описание сервера.")
        tag = item.get("tag")
        if tag is None:
            # Unreferenced service entries need not carry a tag in sing-box.
            continue
        if not isinstance(tag, str) or not tag or tag in result:
            raise SourceGroupError(
                "Теги исходных серверов отсутствуют или повторяются."
            )
        result[tag] = item
    return result


def _convert_group(candidates: list[dict[str, Any]]) -> ProxySelection:
    servers, warnings, names = [], [], []
    for index, candidate in enumerate(candidates, 1):
        # Ordinals identify source rows without echoing arbitrary tags/secrets.
        label = f"Сервер №{index}"
        try:
            native_type = candidate.get("type")
            protocol = candidate.get("protocol")
            if (
                isinstance(native_type, str) and native_type in SERVICE_OUTBOUND_TYPES
            ) or (
                isinstance(protocol, str)
                and protocol in {"freedom", "blackhole", "dns", "loopback"}
            ):
                raise ConfigPatchError("service outbound")
            stream = candidate.get("streamSettings", {})
            socket_options = (
                stream.get("sockopt", {}) if isinstance(stream, dict) else {}
            )
            if any(
                candidate.get(key) for key in ("detour", "proxySettings", "dialerProxy")
            ) or (
                isinstance(socket_options, dict) and socket_options.get("dialerProxy")
            ):
                raise ConfigPatchError("dependent outbound")
            converted = normalize_outbound_to_singbox(candidate)
            _validate_group_connection(converted)
            if (
                not isinstance(converted.get("server"), str)
                or not converted["server"].strip()
            ):
                raise ConfigPatchError("missing server")
            port = converted.get("server_port")
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                raise ConfigPatchError("invalid server port")
        except ConfigPatchError as error:
            reason = "неподдерживаемые или некорректные параметры подключения"
            if isinstance(error, UnsupportedVlessTransportError) and error.network:
                reason = f"транспорт {error.network.upper()} не поддерживается"
            warnings.append(f"{label} пропущен: {reason}.")
        else:
            # Display metadata belongs in the selected tag, not in sing-box fields.
            for key in ("remarks", "remark", "name"):
                converted.pop(key, None)
            servers.append(converted)
            names.append(_server_name(candidate, index))
    if not servers:
        raise SourceGroupError(
            "В основной группе нет совместимых серверов. Импорт невозможен."
        )
    return ProxySelection(
        tuple(servers), automatic=True, warnings=tuple(warnings), names=tuple(names)
    )


def _server_name(candidate: dict[str, Any], index: int) -> str:
    for key in ("remarks", "remark", "name", "tag"):
        value = candidate.get(key)
        if not isinstance(value, str):
            continue
        # Keep Unicode names/emoji, but do not introduce log lines or terminal controls.
        value = "".join(
            " " if char.isspace() else char
            for char in normalize("NFC", value)
            if category(char)[0] != "C"
            or char.isspace()
            or char in {"\u200c", "\u200d"}
        ).strip()[:128]
        if value:
            return value
    return f"Сервер №{index}"


def _validate_group_connection(outbound: dict[str, Any]) -> None:
    if outbound.get("type") not in GROUP_PROXY_TYPES:
        raise ConfigPatchError("unsupported group connection")
    if outbound["type"] == "vless":
        value = outbound.get("uuid")
        try:
            if not isinstance(value, str):
                raise ValueError
            UUID(value)
        except ValueError:
            raise ConfigPatchError("invalid VLESS UUID") from None
    elif not isinstance(outbound.get("password"), str) or not outbound["password"]:
        raise ConfigPatchError("missing proxy password")
