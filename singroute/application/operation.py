"""Application use cases for preparing sing-box config updates."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from singroute.core.converters import normalize_outbound_to_singbox
from singroute.core.errors import ConfigParseError
from singroute.core.patcher import (
    patch_router_config,
    select_exported_outbound,
)
from singroute.core.preview import summarize_outbound


@dataclass(frozen=True)
class ConfigUpdate:
    updated_config: dict[str, Any]
    preview: dict[str, Any]


def summarize_router_outbound(router_config_content: str) -> dict[str, Any]:
    """Parse and mask the outbound currently selected for replacement on a router."""
    router_config = _loads_config(router_config_content, "current_router_config")
    return summarize_outbound(_get_first_router_outbound(router_config))


def prepare_config_update(
    imported_config_content: str,
    current_router_config_content: str,
) -> ConfigUpdate:
    """Prepare an outbound update from raw config content without file or SSH I/O."""
    imported_config = _loads_config(imported_config_content, "imported_config")
    current_router_config = _loads_config(
        current_router_config_content,
        "current_router_config",
    )

    old_outbound = _get_first_router_outbound(current_router_config)
    imported_outbound = select_exported_outbound(imported_config)
    new_outbound = normalize_outbound_to_singbox(imported_outbound)
    updated_config = patch_router_config(current_router_config, new_outbound)

    old_outbound_summary = summarize_outbound(old_outbound)
    new_outbound_summary = summarize_outbound(updated_config["outbounds"][0])

    return ConfigUpdate(
        updated_config=updated_config,
        preview={
            "old_outbound": old_outbound_summary,
            "new_outbound": new_outbound_summary,
        },
    )


def _loads_config(config_text: str, config_name: str) -> Any:
    try:
        return json.loads(config_text)
    except json.JSONDecodeError as error:
        raise ConfigParseError(
            f"{config_name} contains invalid JSON: line {error.lineno}, "
            f"column {error.colno}"
        ) from error


def _get_first_router_outbound(router_config: Any) -> dict[str, Any]:
    if not isinstance(router_config, dict):
        return {}

    outbounds = router_config.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        return {}

    first_outbound = outbounds[0]
    if not isinstance(first_outbound, dict):
        return {}

    return first_outbound
