"""Application use cases for preparing sing-box config updates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from singroute.core.auto_outbounds import active_selection, replace_selection
from singroute.core.errors import ConfigParseError, ConfigPatchError
from singroute.core.preview import summarize_outbound
from singroute.core.share_links import parse_share_link
from singroute.core.source_selection import ProxySelection, select_source

MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 128


@dataclass(frozen=True)
class ConfigUpdate:
    updated_config: dict[str, Any]
    preview: dict[str, Any]
    has_changes: bool
    warnings: tuple[str, ...] = ()


def summarize_router_outbound(router_config_content: str) -> dict[str, Any]:
    """Parse and mask the outbound currently selected for replacement on a router."""
    router_config = _loads_config(router_config_content, "current_router_config")
    _get_first_router_outbound(router_config)
    return summarize_outbound(active_selection(router_config))


def prepare_config_update(
    imported_config_content: str,
    current_router_config_content: str,
) -> ConfigUpdate:
    """Prepare an outbound update from raw config content without file or SSH I/O."""
    selection = parse_imported_selection(imported_config_content)
    current_router_config = _loads_config(
        current_router_config_content,
        "current_router_config",
    )

    _get_first_router_outbound(current_router_config)
    updated_config = replace_selection(current_router_config, selection)

    old_outbound_summary = summarize_outbound(active_selection(current_router_config))
    new_outbound_summary = summarize_outbound(active_selection(updated_config))

    return ConfigUpdate(
        updated_config=updated_config,
        preview={
            "old_outbound": old_outbound_summary,
            "new_outbound": new_outbound_summary,
        },
        has_changes=current_router_config["outbounds"] != updated_config["outbounds"],
        warnings=selection.warnings,
    )


def validate_imported_config(config_text: str) -> ProxySelection:
    """Check that source text contains a selectable supported proxy outbound."""
    return parse_imported_selection(config_text)


def parse_imported_outbound(config_text: str) -> dict[str, Any]:
    """Return the first connection from an import (legacy single-server helper)."""
    return parse_imported_selection(config_text).servers[0]


def parse_imported_selection(config_text: str) -> ProxySelection:
    """Accept JSON exports or a single share link through the same workflow."""
    if config_text_exceeds_limit(config_text):
        raise ConfigParseError("imported_config exceeds the safe limit of 8 MiB")
    text = config_text.lstrip("\ufeff").strip()
    if not text.startswith(("{", "[")) and "://" in text:
        return ProxySelection((parse_share_link(text),))
    return select_source(_loads_config(config_text.lstrip("\ufeff"), "imported_config"))


def config_text_exceeds_limit(config_text: str) -> bool:
    """Return whether UTF-8 config content exceeds the accepted input size."""
    return len(config_text.encode("utf-8")) > MAX_CONFIG_BYTES


def _loads_config(config_text: str, config_name: str) -> Any:
    if config_text_exceeds_limit(config_text):
        raise ConfigParseError(
            f"{config_name} exceeds the safe limit of "
            f"{MAX_CONFIG_BYTES // (1024 * 1024)} MiB"
        )
    _validate_json_nesting(config_text, config_name)
    try:
        return json.loads(config_text)
    except json.JSONDecodeError as error:
        raise ConfigParseError(
            f"{config_name} contains invalid JSON: line {error.lineno}, "
            f"column {error.colno}"
        ) from error
    except RecursionError as error:
        raise ConfigParseError(
            f"{config_name} contains excessively nested JSON"
        ) from error


def _validate_json_nesting(config_text: str, config_name: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in config_text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ConfigParseError(
                    f"{config_name} contains excessively nested JSON"
                )
        elif character in "]}":
            depth = max(0, depth - 1)


def _get_first_router_outbound(router_config: Any) -> dict[str, Any]:
    if not isinstance(router_config, dict):
        raise ConfigPatchError("router_config должен быть объектом JSON (dict)")

    outbounds = router_config.get("outbounds")
    if not isinstance(outbounds, list):
        raise ConfigPatchError(
            'router_config["outbounds"] должен быть массивом JSON (list)'
        )
    if not outbounds:
        raise ConfigPatchError('router_config["outbounds"] не должен быть пустым')

    first_outbound = outbounds[0]
    if not isinstance(first_outbound, dict):
        raise ConfigPatchError(
            'router_config["outbounds"][0] должен быть объектом JSON (dict)'
        )

    tag = first_outbound.get("tag")
    if not isinstance(tag, str) or not tag.strip():
        raise ConfigPatchError(
            'router_config["outbounds"][0]["tag"] должен быть непустой строкой'
        )

    return first_outbound
