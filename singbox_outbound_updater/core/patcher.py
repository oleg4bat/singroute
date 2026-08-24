"""Functions for selecting exported outbounds and patching router configs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import ConfigPatchError


SERVICE_OUTBOUND_TYPES = {"direct", "block", "dns", "selector", "urltest"}
SERVICE_XRAY_PROTOCOLS = {"freedom", "blackhole", "dns"}
SUPPORTED_XRAY_PROTOCOLS = {"vless", "hysteria"}


def select_exported_outbound(
    exported_config: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of the first supported proxy outbound."""
    outbounds = _get_exported_outbounds(exported_config)

    for index, outbound in enumerate(outbounds):
        _ensure_outbound_dict(outbound, index)
        if _is_supported_proxy_outbound(outbound):
            return deepcopy(outbound)

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
        if protocol in SERVICE_XRAY_PROTOCOLS:
            return False
        if protocol not in SUPPORTED_XRAY_PROTOCOLS:
            return False
        return _is_supported_xray_protocol_outbound(outbound, protocol)

    return False


def _is_supported_xray_protocol_outbound(
    outbound: dict[str, Any],
    protocol: str,
) -> bool:
    if protocol == "vless":
        stream_settings = outbound.get("streamSettings")
        if not isinstance(stream_settings, dict):
            return False
        return (
            stream_settings.get("network") == "tcp"
            and stream_settings.get("security") == "reality"
        )

    if protocol == "hysteria":
        settings = outbound.get("settings")
        stream_settings = outbound.get("streamSettings", {})
        if stream_settings is None:
            stream_settings = {}
        if not isinstance(settings, dict) or not isinstance(stream_settings, dict):
            return False
        hysteria_settings = stream_settings.get("hysteriaSettings", {})
        if hysteria_settings is None:
            hysteria_settings = {}
        if not isinstance(hysteria_settings, dict):
            return False
        return settings.get("version") == 2 or hysteria_settings.get("version") == 2

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
    if outbounds is None:
        outbounds = []
        patched_config["outbounds"] = outbounds
    elif not isinstance(outbounds, list):
        raise ConfigPatchError('router_config["outbounds"] must be a JSON array (list)')

    if outbounds:
        old_first_outbound = outbounds[0]
        if isinstance(old_first_outbound, dict) and "tag" in old_first_outbound:
            replacement_outbound["tag"] = old_first_outbound["tag"]

        outbounds[0] = replacement_outbound
    else:
        outbounds.append(replacement_outbound)

    return patched_config
