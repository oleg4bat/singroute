"""Functions for selecting exported outbounds and patching router configs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .converters import normalize_outbound_to_singbox
from .errors import ConfigPatchError

SERVICE_OUTBOUND_TYPES = {"direct", "block", "dns", "selector", "urltest"}
SUPPORTED_XRAY_PROTOCOLS = {"vless", "hysteria"}


def select_exported_outbound(
    exported_config: dict[str, Any],
) -> dict[str, Any]:
    """Return the first valid supported proxy normalized for sing-box."""
    outbounds = _get_exported_outbounds(exported_config)
    first_invalid_error: ConfigPatchError | None = None

    for index, outbound in enumerate(outbounds):
        _ensure_outbound_dict(outbound, index)
        if not _is_supported_proxy_outbound(outbound):
            continue
        try:
            return normalize_outbound_to_singbox(outbound)
        except ConfigPatchError as error:
            first_invalid_error = first_invalid_error or error

    if first_invalid_error is not None:
        raise first_invalid_error

    raise ConfigPatchError(
        "exported_config не содержит поддерживаемый proxy outbound "
        "(служебные direct, block, dns, selector, urltest не подходят)"
    )


def _get_exported_outbounds(exported_config: dict[str, Any]) -> list[Any]:
    if not isinstance(exported_config, dict):
        raise ConfigPatchError("exported_config должен быть объектом JSON (dict)")

    outbounds = exported_config.get("outbounds")
    if not isinstance(outbounds, list):
        raise ConfigPatchError('exported_config должен содержать массив "outbounds"')

    if not outbounds:
        raise ConfigPatchError('exported_config["outbounds"] не должен быть пустым')

    return outbounds


def _ensure_outbound_dict(outbound: Any, index: int) -> None:
    if not isinstance(outbound, dict):
        raise ConfigPatchError(
            f'exported_config["outbounds"][{index}] должен быть объектом JSON (dict)'
        )


def _is_supported_proxy_outbound(outbound: dict[str, Any]) -> bool:
    outbound_type = outbound.get("type")
    if isinstance(outbound_type, str):
        return outbound_type not in SERVICE_OUTBOUND_TYPES

    protocol = outbound.get("protocol")
    if isinstance(protocol, str):
        return protocol in SUPPORTED_XRAY_PROTOCOLS

    return False


def patch_router_config(
    router_config: dict[str, Any],
    new_outbound: dict[str, Any],
) -> dict[str, Any]:
    """Replace the first outbound while preserving its routing tag."""
    if not isinstance(router_config, dict):
        raise ConfigPatchError("router_config должен быть объектом JSON (dict)")

    if not isinstance(new_outbound, dict):
        raise ConfigPatchError("new_outbound должен быть объектом JSON (dict)")

    patched_config = deepcopy(router_config)
    replacement_outbound = deepcopy(new_outbound)

    outbounds = patched_config.get("outbounds")
    if not isinstance(outbounds, list):
        raise ConfigPatchError('router_config["outbounds"] must be a JSON array (list)')
    if not outbounds:
        raise ConfigPatchError('router_config["outbounds"] must not be empty')

    old_first_outbound = outbounds[0]
    if not isinstance(old_first_outbound, dict):
        raise ConfigPatchError(
            'router_config["outbounds"][0] must be a JSON object (dict)'
        )
    old_tag = old_first_outbound.get("tag")
    if not isinstance(old_tag, str) or not old_tag.strip():
        raise ConfigPatchError(
            'router_config["outbounds"][0]["tag"] must be a non-empty string'
        )

    replacement_outbound["tag"] = old_tag
    outbounds[0] = replacement_outbound

    return patched_config
