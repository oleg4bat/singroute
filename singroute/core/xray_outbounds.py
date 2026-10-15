"""Select and replace an Xray server exit without importing client routing."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .auto_outbounds import _references_tags
from .converters.xray import to_xray
from .errors import ConfigPatchError, SourceGroupError
from .source_selection import (
    _strings,
    _tagged_outbounds,
    _xray_primary_balancer,
    select_source,
)

PROXY_PROTOCOLS = {
    "vless",
    "vmess",
    "trojan",
    "shadowsocks",
    "socks",
    "http",
    "hysteria",
    "wireguard",
}
MONITORS = ("observatory", "burstObservatory")


@dataclass(frozen=True)
class XraySelection:
    servers: tuple[dict[str, Any], ...]
    balancer: dict[str, Any] | None = None
    monitors: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    dependent_balancers: tuple[dict[str, Any], ...] = ()
    internal_rules: tuple[dict[str, Any], ...] = ()


def _native_server(item: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(item.get("protocol"), str)
        or item.get("protocol") not in PROXY_PROTOCOLS
        or not isinstance(item.get("settings"), dict)
    ):
        raise ConfigPatchError("Некорректный или неподдерживаемый сервер Xray.")
    stream = item.get("streamSettings", {})
    if not isinstance(stream, dict):
        raise ConfigPatchError("Некорректный транспорт Xray.")
    sockopt = stream.get("sockopt", {})
    if (
        not isinstance(sockopt, dict)
        or item.get("proxySettings")
        or sockopt.get("dialerProxy")
    ):
        raise ConfigPatchError(
            "Сервер зависит от других выходов. Такой перенос пока не поддерживается."
        )
    if "reverse" in item["settings"]:
        raise ConfigPatchError(
            "Обратный прокси нельзя импортировать как обычный сервер."
        )
    allowed = {
        "tag",
        "protocol",
        "settings",
        "streamSettings",
        "mux",
        "sendThrough",
        "targetStrategy",
    }
    if set(item) - allowed - {"remarks", "remark", "name"}:
        raise ConfigPatchError("Сервер содержит неподдерживаемые зависимости Xray.")
    return deepcopy({key: value for key, value in item.items() if key in allowed})


def select_xray_source(config: dict[str, Any]) -> XraySelection:
    if not isinstance(config, dict):
        raise ConfigPatchError("Исходный конфиг должен быть объектом JSON.")
    outbounds = config.get("outbounds")
    if (
        not isinstance(outbounds, list)
        or not outbounds
        or not all(isinstance(o, dict) for o in outbounds)
    ):
        raise ConfigPatchError("Исходный конфиг должен содержать серверы.")
    if any("type" in o for o in outbounds):
        selection = select_source(config)
        servers = tuple(to_xray(server) for server in selection.servers)
        if not selection.automatic:
            return XraySelection(servers, warnings=selection.warnings)
        nodes = []
        for index, server in enumerate(servers, 1):
            nodes.append({**server, "tag": f"singroute-node-{index}"})
        return XraySelection(
            tuple(nodes),
            {
                "tag": "singroute-auto",
                "selector": ["singroute-node-"],
                "fallbackTag": nodes[0]["tag"],
                "strategy": {"type": "leastPing"},
            },
            {
                "observatory": {
                    "subjectSelector": ["singroute-node-"],
                    "probeURL": "https://www.gstatic.com/generate_204",
                    "probeInterval": "1m",
                    "enableConcurrency": True,
                }
            },
            (
                *selection.warnings,
                "Группа sing-box преобразована в автоматический выбор Xray по задержке.",
            ),
        )
    routing = config.get("routing", {})
    if not isinstance(routing, dict):
        raise SourceGroupError("Некорректная маршрутизация Xray.")
    balancers = routing.get("balancers", [])
    if balancers:
        primary = _xray_primary_balancer(config, routing, balancers)
        tagged = _tagged_outbounds(config["outbounds"])
        selected_tags, group_tags = set(), set()
        selected_groups: dict[str, dict[str, Any]] = {}
        internal_rules = []

        def visit_group(group: dict[str, Any], stack: set[str]) -> None:
            tag = group.get("tag")
            if not isinstance(tag, str) or not tag:
                raise SourceGroupError("Некорректный тег группы Xray.")
            if tag in stack:
                raise SourceGroupError("Резервные группы Xray образуют цикл.")
            if tag in selected_groups:
                return
            if sum(b.get("tag") == tag for b in balancers) != 1:
                raise SourceGroupError("Теги групп Xray повторяются.")
            prefixes = _strings(group.get("selector"))
            members = {t for t in tagged if any(t.startswith(p) for p in prefixes)}
            if not members:
                raise SourceGroupError("В группе Xray отсутствуют серверы.")
            selected_groups[tag] = deepcopy(group)
            strategy = group.get("strategy", {})
            if not isinstance(strategy, dict):
                raise SourceGroupError("Некорректная стратегия группы.")
            if strategy.get("type") in {"leastPing", "leastLoad"}:
                group_tags.update(members)
            fallback = group.get("fallbackTag")
            if fallback is not None:
                if not isinstance(fallback, str) or fallback not in tagged:
                    raise SourceGroupError("Резервный сервер группы отсутствует.")
                members.add(fallback)
            for member in members:
                entry = tagged[member]
                selected_tags.add(member)
                if entry.get("protocol") != "loopback":
                    _native_server(entry)
                    continue
                settings = entry.get("settings", {})
                if (
                    set(entry)
                    - {"tag", "protocol", "settings", "remarks", "remark", "name"}
                    or not isinstance(settings, dict)
                    or set(settings) != {"inboundTag"}
                    or not isinstance(settings["inboundTag"], str)
                    or not settings["inboundTag"]
                ):
                    raise SourceGroupError("Некорректный резервный переход Xray.")
                inbound = settings["inboundTag"]
                rules = [
                    r
                    for r in routing.get("rules", [])
                    if inbound in r.get("inboundTag", [])
                ]
                if (
                    len(rules) != 1
                    or not _default_rule(rules[0])
                    or rules[0].get("inboundTag") != [inbound]
                    or rules[0].get("outboundTag")
                ):
                    raise SourceGroupError(
                        "Неоднозначная маршрутизация резервной группы Xray."
                    )
                rule = rules[0]
                matches = [
                    b for b in balancers if b.get("tag") == rule.get("balancerTag")
                ]
                if len(matches) != 1:
                    raise SourceGroupError(
                        "Резервная группа Xray отсутствует или неоднозначна."
                    )
                if rule not in internal_rules:
                    internal_rules.append(deepcopy(rule))
                visit_group(matches[0], stack | {tag})

        visit_group(primary, set())
        balancer = deepcopy(primary)
        servers = tuple(
            {
                k: deepcopy(v)
                for k, v in item.items()
                if k in {"tag", "protocol", "settings"}
            }
            if item.get("protocol") == "loopback"
            else _native_server(item)
            for item in config["outbounds"]
            if item.get("tag") in selected_tags
        )
        monitors = {}
        for key in MONITORS:
            if key not in config:
                continue
            monitor = config[key]
            if not isinstance(monitor, dict):
                raise SourceGroupError("Некорректная проверка доступности серверов.")
            prefixes = _strings(monitor.get("subjectSelector"))
            relevant = [
                p for p in prefixes if any(t.startswith(p) for t in selected_tags)
            ]
            if relevant:
                if any(
                    t not in selected_tags and any(t.startswith(p) for p in relevant)
                    for t in tagged
                ):
                    raise SourceGroupError(
                        "Проверка доступности используется другими серверами."
                    )
                monitors[key] = {**deepcopy(monitor), "subjectSelector": relevant}
        if group_tags:
            coverage = {
                t
                for t in selected_tags
                if any(
                    t.startswith(p)
                    for m in monitors.values()
                    for p in m["subjectSelector"]
                )
            }
            if not group_tags <= coverage:
                raise SourceGroupError(
                    "В исходном JSON отсутствуют настройки наблюдения для части серверов группы."
                )
        return XraySelection(
            servers,
            balancer,
            monitors,
            dependent_balancers=tuple(
                b for t, b in selected_groups.items() if t != balancer["tag"]
            ),
            internal_rules=tuple(internal_rules),
        )
    rules = routing.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise ConfigPatchError("Некорректные правила Xray.")
    default_tags = {
        r["outboundTag"]
        for r in rules
        if isinstance(r.get("outboundTag"), str) and _default_rule(r)
    }
    if len(default_tags) > 1:
        raise ConfigPatchError("Основной выход Xray неоднозначен.")
    if default_tags:
        chosen = [
            o
            for o in outbounds
            if isinstance(o.get("tag"), str) and o.get("tag") in default_tags
        ]
        if len(chosen) != 1:
            raise ConfigPatchError("Основной выход Xray отсутствует или неоднозначен.")
        return XraySelection((_native_server(chosen[0]),))
    for item in outbounds:
        if (
            isinstance(item.get("protocol"), str)
            and item.get("protocol") in PROXY_PROTOCOLS
        ):
            return XraySelection((_native_server(item),))
    raise ConfigPatchError("В конфиге нет поддерживаемого сервера Xray.")


def _default_rule(rule: dict[str, Any]) -> bool:
    return (
        not (
            set(rule)
            - {"type", "network", "inboundTag", "balancerTag", "outboundTag", "ruleTag"}
        )
        and isinstance(rule.get("network", "tcp,udp"), str)
        and rule.get("network", "tcp,udp") in {"tcp,udp", "udp,tcp"}
    )


def current_xray_selection(config: dict[str, Any]) -> XraySelection | None:
    if not isinstance(config, dict) or not isinstance(config.get("outbounds"), list):
        raise ConfigPatchError("Некорректный конфиг Xray на роутере.")
    if not config["outbounds"]:
        return None
    if not all(isinstance(item, dict) for item in config["outbounds"]):
        raise ConfigPatchError("Некорректные выходы Xray на роутере.")
    tags = [item.get("tag") for item in config["outbounds"] if "tag" in item]
    if any(not isinstance(tag, str) or not tag for tag in tags) or len(tags) != len(
        set(tags)
    ):
        raise ConfigPatchError("Теги выходов роутера должны быть уникальными строками.")
    routing = config.get("routing", {})
    if not isinstance(routing, dict):
        raise ConfigPatchError("Некорректная маршрутизация Xray на роутере.")
    rules = routing.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise ConfigPatchError("Некорректные правила Xray на роутере.")
    if not routing.get("balancers") and not any(
        _default_rule(r) and r.get("outboundTag") for r in rules
    ):
        first = config["outbounds"][0]
        if isinstance(first.get("protocol"), str) and first.get("protocol") in {
            "freedom",
            "blackhole",
            "dns",
        }:
            return None
    return select_xray_source(config)


def selection_preview(selection: XraySelection | None) -> dict[str, Any]:
    if selection is None:
        return {"outbounds": []}
    if selection.balancer is None:
        return selection.servers[0]
    preview = {
        "outbounds": list(selection.servers),
        "balancer": selection.balancer,
        **selection.monitors,
    }
    if selection.dependent_balancers:
        preview["dependent_balancers"] = list(selection.dependent_balancers)
    return preview


def xray_preview(config: dict[str, Any]) -> dict[str, Any]:
    selection = current_xray_selection(config)
    preview = selection_preview(selection)
    if selection and selection.balancer:
        preview["routing"] = {
            "rules": [
                r
                for r in config.get("routing", {}).get("rules", [])
                if r.get("balancerTag")
                in {
                    selection.balancer["tag"],
                    *(b["tag"] for b in selection.dependent_balancers),
                }
            ]
        }
    return preview


def replace_xray_selection(
    config: dict[str, Any], selection: XraySelection
) -> dict[str, Any]:
    old = current_xray_selection(config)
    result = deepcopy(config)
    routing = result.get("routing", {})
    if not isinstance(routing, dict):
        raise ConfigPatchError("Некорректная маршрутизация роутера.")
    rules, balancers = routing.get("rules", []), routing.get("balancers", [])
    if (
        not isinstance(rules, list)
        or not all(isinstance(r, dict) for r in rules)
        or not isinstance(balancers, list)
    ):
        raise ConfigPatchError("Некорректная маршрутизация роутера.")
    if not all(isinstance(b, dict) for b in balancers):
        raise ConfigPatchError("Некорректные группы Xray на роутере.")
    old_tags = {s.get("tag") for s in old.servers if s.get("tag")} if old else set()
    old_balancer = old.balancer["tag"] if old and old.balancer else None
    old_group_tags = (
        {old_balancer, *(b["tag"] for b in old.dependent_balancers)}
        if old_balancer
        else set()
    )
    if old and old.internal_rules:
        rules = [r for r in rules if r not in old.internal_rules]
        routing["rules"] = rules
    old_tag = old.servers[0].get("tag", "proxy") if old else "proxy"
    preserved = [
        s
        for s in result["outbounds"]
        if s.get("tag") not in old_tags
        and not (old and old.balancer is None and s == old.servers[0])
    ]
    insertion = 0
    if old:
        for item in result["outbounds"]:
            if item not in preserved:
                break
            insertion += 1
    # Shared group members and monitoring must never be deleted as collateral.
    protected = {
        key: value
        for key, value in result.items()
        if key not in {"outbounds", "routing", *MONITORS}
    }
    protected["outbounds"] = preserved
    other_balancers = [b for b in balancers if b.get("tag") not in old_group_tags]
    protected["balancers"] = other_balancers
    protected["rules"] = [
        r
        for r in rules
        if not (
            r.get("balancerTag") == old_balancer
            if old_balancer
            else r.get("outboundTag") == old_tag
        )
    ]
    if old_balancer and (
        _references_tags(protected, old_tags)
        or any(r.get("balancerTag") in old_group_tags for r in protected["rules"])
        or any(
            any(t.startswith(p) for t in old_tags for p in b.get("selector", []))
            for b in other_balancers
        )
    ):
        raise ConfigPatchError(
            "Серверы прежней группы используются в других настройках."
        )
    for key in MONITORS:
        existing = result.get(key)
        if existing is None:
            continue
        prefixes = (
            _strings(existing.get("subjectSelector"))
            if isinstance(existing, dict)
            else []
        )
        covered = {
            s.get("tag")
            for s in config["outbounds"]
            if isinstance(s.get("tag"), str)
            and any(s["tag"].startswith(p) for p in prefixes)
        }
        if covered and covered <= old_tags and old_balancer:
            result.pop(key)
        elif key in selection.monitors:
            raise ConfigPatchError(
                "Проверка доступности роутера используется другими выходами. Замена остановлена."
            )
    if selection.balancer:
        new_balancer = deepcopy(selection.balancer)
        new_balancer["tag"] = old_balancer or "singroute-auto"
        new_groups = [new_balancer, *deepcopy(list(selection.dependent_balancers))]
        new_group_tags = [b["tag"] for b in new_groups]
        if len(set(new_group_tags)) != len(new_group_tags) or any(
            b.get("tag") in new_group_tags for b in other_balancers
        ):
            raise ConfigPatchError("Тег новой группы занят на роутере.")
        servers = deepcopy(list(selection.servers))
        tags = {s.get("tag") for s in servers}
        if len(tags) != len(servers) or None in tags:
            raise ConfigPatchError("Серверы группы должны иметь уникальные теги.")
        if any(
            s.get("tag") in tags
            or any(
                isinstance(s.get("tag"), str) and s["tag"].startswith(p)
                for b in new_groups
                for p in b["selector"]
            )
            for s in preserved
        ):
            raise ConfigPatchError(
                "Теги или префиксы новой группы пересекаются с другими выходами роутера."
            )
        for monitor in selection.monitors.values():
            if any(
                isinstance(s.get("tag"), str)
                and any(s["tag"].startswith(p) for p in monitor["subjectSelector"])
                for s in preserved
            ):
                raise ConfigPatchError(
                    "Проверка доступности затрагивает другие выходы роутера."
                )
        internal_inbounds = {
            s["settings"]["inboundTag"]
            for s in servers
            if s.get("protocol") == "loopback"
        }
        if (
            any(i.get("tag") in internal_inbounds for i in result.get("inbounds", []))
            or any(_references_tags(s, internal_inbounds) for s in preserved)
            or any(
                internal_inbounds.intersection(r.get("inboundTag", [])) for r in rules
            )
        ):
            raise ConfigPatchError(
                "Резервный переход пересекается с другими входами роутера."
            )
        found = False
        for rule in rules:
            if (old_balancer and rule.get("balancerTag") == old_balancer) or (
                not old_balancer and rule.get("outboundTag") == old_tag
            ):
                rule.pop("outboundTag", None)
                rule["balancerTag"] = new_balancer["tag"]
                found = found or _default_rule(rule)
        if not found and old and insertion != 0:
            # A scoped proxy rule must not become the global route for other inputs.
            found = any(r.get("balancerTag") == new_balancer["tag"] for r in rules)
        # Internal loopback rules must precede a catch-all primary rule.
        insertion_rule = next(
            (
                i
                for i, r in enumerate(rules)
                if r.get("balancerTag") == new_balancer["tag"] and _default_rule(r)
            ),
            len(rules),
        )
        rules[insertion_rule:insertion_rule] = deepcopy(list(selection.internal_rules))
        if not found:
            rules.append(
                {
                    "type": "field",
                    "network": "tcp,udp",
                    "balancerTag": new_balancer["tag"],
                }
            )
        routing["balancers"] = [*other_balancers, *new_groups]
        routing["rules"] = rules
        result["routing"] = routing
        result.update(deepcopy(selection.monitors))
    else:
        server = deepcopy(selection.servers[0])
        server["tag"] = old_tag
        if any(s.get("tag") == old_tag for s in preserved):
            raise ConfigPatchError("Тег сервера занят другим выходом роутера.")
        servers = [server]
        if old_balancer:
            for rule in rules:
                if rule.get("balancerTag") == old_balancer:
                    rule.pop("balancerTag")
                    rule["outboundTag"] = old_tag
            if other_balancers:
                routing["balancers"] = other_balancers
            else:
                routing.pop("balancers", None)
    result["outbounds"] = [*preserved[:insertion], *servers, *preserved[insertion:]]
    return result
