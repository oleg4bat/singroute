"""Replace an owned automatic exit while preserving all unrelated config data."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from .errors import ConfigPatchError
from .outbound_order import order_outbound
from .patcher import patch_router_config
from .source_selection import ProxySelection

AUTO_TEST_SETTINGS = {
    "url": "https://www.gstatic.com/generate_204",
    "interval": "1m",
    "tolerance": 20,
    "interrupt_exist_connections": True,
}


def _member_prefix(active_tag: str) -> str:
    digest = hashlib.sha256(active_tag.encode("utf-8")).hexdigest()[:16]
    return f"singroute-auto-{digest}-"


def managed_member_tags(config: dict[str, Any]) -> set[str]:
    outbounds = config.get("outbounds", [])
    if (
        not isinstance(outbounds, list)
        or not outbounds
        or not isinstance(outbounds[0], dict)
    ):
        return set()
    active = outbounds[0]
    if active.get("type") != "urltest" or not isinstance(active.get("tag"), str):
        return set()
    members = active.get("outbounds")
    if not isinstance(members, list) or not members:
        return set()
    expected = [
        _member_prefix(active["tag"]) + str(i) for i in range(1, len(members) + 1)
    ]
    # v0.10.0 used opaque numbered tags. New groups use source names and the
    # exact policy shape emitted below; shared nodes are still protected below.
    named_group = set(active) == {
        "type",
        "tag",
        "outbounds",
        *AUTO_TEST_SETTINGS,
    } and all(active.get(key) == value for key, value in AUTO_TEST_SETTINGS.items())
    if members != expected and not named_group:
        return set()
    if (
        any(not isinstance(tag, str) or not tag for tag in members)
        or len(set(members)) != len(members)
        or active["tag"] in members
    ):
        raise ConfigPatchError(
            "Не удалось безопасно определить серверы прежней группы."
        )
    tags = set(members)
    for tag in tags:
        matches = [
            item
            for item in outbounds
            if isinstance(item, dict) and item.get("tag") == tag
        ]
        if len(matches) != 1 or matches[0].get("type") in {
            "urltest",
            "selector",
            "direct",
            "block",
            "dns",
        }:
            raise ConfigPatchError(
                "Не удалось безопасно определить серверы прежней группы."
            )
    # Refuse to remove owned nodes reused elsewhere (including dialer/DNS/routes).
    other = {key: value for key, value in config.items() if key != "outbounds"}
    other["outbounds"] = [
        item
        for item in outbounds[1:]
        if not isinstance(item, dict) or item.get("tag") not in tags
    ]
    if _references_tags(other, tags):
        raise ConfigPatchError(
            "Серверы прежней группы используются в других настройках. Замена остановлена."
        )
    return tags


def _references_tags(value: Any, tags: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_references_tags(item, tags) for item in value.values())
    if isinstance(value, list):
        return any(_references_tags(item, tags) for item in value)
    return isinstance(value, str) and value in tags


def replace_selection(
    config: dict[str, Any], selection: ProxySelection
) -> dict[str, Any]:
    # Reuse the existing active-outbound validation and tag preservation.
    result = patch_router_config(config, selection.servers[0])
    old_members = managed_member_tags(config)
    preserved = [
        item
        for item in result["outbounds"][1:]
        if not isinstance(item, dict) or item.get("tag") not in old_members
    ]
    if not selection.automatic:
        result["outbounds"] = [result["outbounds"][0], *preserved]
        return result

    active_tag = result["outbounds"][0]["tag"]
    members = []
    tags = []
    existing = {item.get("tag") for item in preserved if isinstance(item, dict)} | {
        active_tag
    }
    for index, server in enumerate(selection.servers, 1):
        name = (
            selection.names[index - 1]
            if index <= len(selection.names)
            else f"Сервер №{index}"
        )
        tag = name
        suffix = 2
        while tag in existing:
            tag = f"{name} ({suffix})"
            suffix += 1
        existing.add(tag)
        member = deepcopy(server)
        member["tag"] = tag
        members.append(order_outbound(member))
        tags.append(tag)
    active = {
        "type": "urltest",
        "tag": active_tag,
        "outbounds": tags,
        **AUTO_TEST_SETTINGS,
    }
    result["outbounds"] = [active, *members, *preserved]
    return result


def active_selection(config: dict[str, Any]) -> dict[str, Any]:
    """Include leaf connections in previews, never unrelated router settings."""
    active = config["outbounds"][0]
    if active.get("type") not in {"urltest", "selector"}:
        return active
    tags = active.get("outbounds", [])
    return {
        "group": active,
        "servers": [
            item
            for item in config["outbounds"][1:]
            if isinstance(item, dict) and item.get("tag") in tags
        ],
    }
