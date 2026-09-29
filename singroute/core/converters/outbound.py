"""Outbound converters for sing-box and Xray/HAPP-style configs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from singroute.core.errors import (
    ConfigPatchError,
    UnsupportedGrpcSettingError,
    UnsupportedVlessTransportError,
)


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
        return _convert_vless_reality(outbound)

    if protocol == "trojan":
        return _convert_trojan(outbound)

    if protocol == "hysteria":
        return _convert_hysteria2(outbound)

    raise ConfigPatchError(f"Unsupported outbound protocol: {protocol}")


def _convert_vless_reality(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = _require_dict(outbound, "settings")
    stream_settings = _require_dict(outbound, "streamSettings")

    network = _require_value(stream_settings, "network", "streamSettings")
    if network not in ("tcp", "raw", "grpc"):
        raise UnsupportedVlessTransportError(network)

    security = _require_value(stream_settings, "security", "streamSettings")
    if security != "reality":
        raise ConfigPatchError("VLESS conversion supports only Reality security")

    vnext = _require_non_empty_list(settings, "vnext", "settings")
    first_vnext = _require_list_item_dict(vnext, "settings.vnext[0]")
    users = _require_non_empty_list(first_vnext, "users", "settings.vnext[0]")
    first_user = _require_list_item_dict(users, "settings.vnext[0].users[0]")

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
    # Xray streamSettings.network selects the server transport; sing-box network
    # filters proxied traffic, so copying "tcp" here would disable UDP.

    result["tls"] = _convert_reality_tls(stream_settings)
    if network == "grpc":
        result["transport"] = _convert_grpc_transport(stream_settings)
    return result


def _convert_trojan(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = _require_dict(outbound, "settings")
    stream_settings = _require_dict(outbound, "streamSettings")
    if "servers" in settings:
        servers = _require_non_empty_list(settings, "servers", "settings")
        server = _require_list_item_dict(servers, "settings.servers[0]")
        location = "settings.servers[0]"
    else:
        server = settings
        location = "settings"

    network = stream_settings.get("network", "tcp")
    if network not in ("tcp", "raw", "grpc"):
        raise ConfigPatchError("Unsupported Trojan transport")
    if network in ("tcp", "raw"):
        _validate_plain_tcp_settings(stream_settings)
    security = _require_value(stream_settings, "security", "streamSettings")
    if security not in ("tls", "reality"):
        raise ConfigPatchError(
            "Trojan conversion supports only TLS or Reality security"
        )

    result: dict[str, Any] = {"type": "trojan"}
    _copy_optional(outbound, result, "tag")
    result["server"] = deepcopy(_require_value(server, "address", location))
    result["server_port"] = deepcopy(_require_value(server, "port", location))
    result["password"] = deepcopy(_require_value(server, "password", location))
    result["tls"] = (
        _convert_reality_tls(stream_settings)
        if security == "reality"
        else _convert_standard_tls(stream_settings)
    )
    if network == "grpc":
        result["transport"] = _convert_grpc_transport(stream_settings)
    return result


def _convert_reality_tls(stream_settings: dict[str, Any]) -> dict[str, Any]:
    reality_settings = _require_dict(stream_settings, "realitySettings")
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

    return tls


def _convert_standard_tls(stream_settings: dict[str, Any]) -> dict[str, Any]:
    tls_settings = _optional_dict(stream_settings, "tlsSettings")
    for key, value in tls_settings.items():
        if key not in {
            "allowInsecure",
            "serverName",
            "alpn",
            "fingerprint",
        } and value not in (
            None,
            "",
            False,
            0,
            [],
            {},
        ):
            raise ConfigPatchError("Unsupported Trojan TLS setting")
    tls: dict[str, Any] = {
        "enabled": True,
        "insecure": _optional_bool(tls_settings, "allowInsecure", "tlsSettings"),
    }
    for source_key, target_key in (("serverName", "server_name"), ("alpn", "alpn")):
        if source_key in tls_settings:
            tls[target_key] = deepcopy(tls_settings[source_key])
    fingerprint = tls_settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": deepcopy(fingerprint)}
    return tls


def _validate_plain_tcp_settings(stream_settings: dict[str, Any]) -> None:
    tcp_settings = _optional_dict(stream_settings, "tcpSettings")
    header = _optional_dict(tcp_settings, "header")
    if header and header != {"type": "none"}:
        raise ConfigPatchError("Unsupported Trojan TCP header")
    for key, value in tcp_settings.items():
        if key != "header" and value not in (None, "", False, 0, [], {}):
            raise ConfigPatchError("Unsupported Trojan TCP setting")


def _convert_grpc_transport(stream_settings: dict[str, Any]) -> dict[str, Any]:
    grpc_settings = _require_dict(stream_settings, "grpcSettings")
    transport: dict[str, Any] = {"type": "grpc"}
    service_name = grpc_settings.get("serviceName", "")
    if not isinstance(service_name, str):
        raise ConfigPatchError("Invalid string field: grpcSettings.serviceName")
    if service_name:
        transport["service_name"] = service_name

    # sing-box has no separate HTTP/2 authority or Xray TunMulti mode.
    # Reject non-default values so the imported connection is not altered silently.
    for key, value in grpc_settings.items():
        if key == "serviceName":
            continue
        if key == "authority" and not isinstance(value, str):
            raise ConfigPatchError("Invalid string field: grpcSettings.authority")
        if key == "multiMode" and type(value) is not bool:
            raise ConfigPatchError("Invalid boolean field: grpcSettings.multiMode")
        if value not in (None, "", False, 0):
            raise UnsupportedGrpcSettingError(key)
    return transport


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
