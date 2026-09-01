"""Outbound converters for sing-box and Xray/HAPP-style configs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from singroute.core.errors import ConfigPatchError


def normalize_outbound_to_singbox(outbound: dict[str, Any]) -> dict[str, Any]:
    """Return a sing-box-compatible outbound without mutating the input."""
    if not isinstance(outbound, dict):
        raise ConfigPatchError("outbound must be a JSON object (dict)")

    outbound_type = outbound.get("type")
    if isinstance(outbound_type, str):
        return deepcopy(outbound)
    if "type" in outbound:
        raise ConfigPatchError("Invalid field: type must be a string")

    protocol = outbound.get("protocol")
    if protocol is None:
        raise ConfigPatchError(
            "Unsupported outbound format: expected sing-box 'type' or Xray/HAPP 'protocol'"
        )

    if protocol == "vless":
        return _convert_vless_reality_tcp(outbound)

    if protocol == "hysteria":
        return _convert_hysteria2(outbound)

    raise ConfigPatchError(f"Unsupported outbound protocol: {protocol}")


def _convert_vless_reality_tcp(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = _require_dict(outbound, "settings")
    stream_settings = _require_dict(outbound, "streamSettings")

    network = _require_value(stream_settings, "network", "streamSettings")
    if network != "tcp":
        raise ConfigPatchError("VLESS conversion supports only Reality over TCP")

    security = _require_value(stream_settings, "security", "streamSettings")
    if security != "reality":
        raise ConfigPatchError("VLESS conversion supports only Reality security")

    vnext = _require_non_empty_list(settings, "vnext", "settings")
    first_vnext = _require_list_item_dict(vnext, "settings.vnext[0]")
    users = _require_non_empty_list(first_vnext, "users", "settings.vnext[0]")
    first_user = _require_list_item_dict(users, "settings.vnext[0].users[0]")
    reality_settings = _require_dict(stream_settings, "realitySettings")

    result: dict[str, Any] = {"type": "vless"}
    _copy_optional(outbound, result, "tag")
    result.update(
        {
            "server": deepcopy(
                _require_value(first_vnext, "address", "settings.vnext[0]")
            ),
            "server_port": deepcopy(
                _require_value(first_vnext, "port", "settings.vnext[0]")
            ),
            "uuid": deepcopy(
                _require_value(first_user, "id", "settings.vnext[0].users[0]")
            ),
        }
    )
    _copy_optional(first_user, result, "flow")
    result["network"] = "tcp"

    tls: dict[str, Any] = {
        "enabled": True,
        "server_name": deepcopy(
            _require_value(reality_settings, "serverName", "realitySettings")
        ),
        "insecure": _optional_bool(
            reality_settings,
            "allowInsecure",
            "realitySettings",
        ),
        "reality": {
            "enabled": True,
            "public_key": deepcopy(
                _require_value(reality_settings, "publicKey", "realitySettings")
            ),
            "short_id": deepcopy(
                _require_value(reality_settings, "shortId", "realitySettings")
            ),
        },
    }

    fingerprint = reality_settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": deepcopy(fingerprint)}

    result["tls"] = tls
    return result


def _convert_hysteria2(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = _require_dict(outbound, "settings")
    stream_settings = _optional_dict(outbound, "streamSettings")
    hysteria_settings = _optional_dict(stream_settings, "hysteriaSettings")
    tls_settings = _optional_dict(stream_settings, "tlsSettings")

    if settings.get("version") != 2 and hysteria_settings.get("version") != 2:
        raise ConfigPatchError("Only Hysteria2 is supported for protocol hysteria")

    result: dict[str, Any] = {"type": "hysteria2"}
    _copy_optional(outbound, result, "tag")
    result.update(
        {
            "server": deepcopy(_require_value(settings, "address", "settings")),
            "server_port": deepcopy(_require_value(settings, "port", "settings")),
            "password": deepcopy(
                _require_value(hysteria_settings, "auth", "hysteriaSettings")
            ),
        }
    )

    tls: dict[str, Any] = {
        "enabled": True,
        "insecure": _optional_bool(tls_settings, "allowInsecure", "tlsSettings"),
    }
    if "serverName" in tls_settings:
        tls["server_name"] = deepcopy(tls_settings["serverName"])
    if "alpn" in tls_settings:
        tls["alpn"] = deepcopy(tls_settings["alpn"])

    result["tls"] = tls
    return result


def _copy_optional(source: dict[str, Any], target: dict[str, Any], key: str) -> None:
    if key in source:
        target[key] = deepcopy(source[key])


def _require_dict(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ConfigPatchError(f"Missing or invalid object field: {key}")
    return value


def _optional_dict(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigPatchError(f"Invalid object field: {key}")
    return value


def _require_value(source: dict[str, Any], key: str, location: str) -> Any:
    if key not in source or source[key] in (None, ""):
        raise ConfigPatchError(f"Missing required field: {location}.{key}")
    return source[key]


def _optional_bool(
    source: dict[str, Any],
    key: str,
    location: str,
    *,
    default: bool = False,
) -> bool:
    if key not in source:
        return default
    value = source[key]
    if type(value) is not bool:
        raise ConfigPatchError(f"Invalid boolean field: {location}.{key}")
    return value


def _require_non_empty_list(
    source: dict[str, Any], key: str, location: str
) -> list[Any]:
    value = source.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigPatchError(f"Missing or empty list field: {location}.{key}")
    return value


def _require_list_item_dict(items: list[Any], location: str) -> dict[str, Any]:
    item = items[0]
    if not isinstance(item, dict):
        raise ConfigPatchError(f"Invalid object field: {location}")
    return item
