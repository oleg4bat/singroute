"""Outbound converters for sing-box and Xray/HAPP-style configs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import unquote

from singroute.core.errors import (
    ConfigPatchError,
    UnsupportedGrpcSettingError,
    UnsupportedVlessTransportError,
    WebSocketConfigError,
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
        return _convert_vless(outbound)

    if protocol == "trojan":
        return _convert_trojan(outbound)

    if protocol == "hysteria":
        return _convert_hysteria2(outbound)

    raise ConfigPatchError(f"Unsupported outbound protocol: {protocol}")


def _convert_vless(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = _require_dict(outbound, "settings")
    stream_settings = _require_dict(outbound, "streamSettings")

    network = _require_value(stream_settings, "network", "streamSettings")
    if network not in ("tcp", "raw", "grpc", "ws"):
        raise UnsupportedVlessTransportError(network)
    if network in ("tcp", "raw"):
        _validate_plain_tcp_settings(stream_settings)

    security = _require_value(stream_settings, "security", "streamSettings")
    if security not in ("reality", "tls", "none"):
        raise ConfigPatchError("Unsupported VLESS security")
    if network == "ws" and security == "reality":
        raise WebSocketConfigError("WebSocket с Reality не поддерживается.")

    if "vnext" in settings:
        vnext = _require_non_empty_list(settings, "vnext", "settings")
        server = _require_list_item_dict(vnext, "settings.vnext[0]")
        users = _require_non_empty_list(server, "users", "settings.vnext[0]")
        user = _require_list_item_dict(users, "settings.vnext[0].users[0]")
    else:
        # HAPP also exports connection fields directly in settings.
        server = user = settings

    if user.get("encryption", "none") != "none":
        raise ConfigPatchError("Unsupported VLESS encryption")
    flow = user.get("flow", "")
    if flow not in ("", "xtls-rprx-vision"):
        raise ConfigPatchError("Unsupported VLESS flow")
    if network == "ws" and flow:
        raise WebSocketConfigError("WebSocket с непустым VLESS flow не поддерживается.")

    result: dict[str, Any] = {"type": "vless"}
    _copy_optional(outbound, result, "tag")
    result.update(
        {
            "server": deepcopy(_require_value(server, "address", "settings")),
            "server_port": deepcopy(_require_value(server, "port", "settings")),
            "uuid": deepcopy(_require_value(user, "id", "settings")),
        }
    )
    if flow:
        result["flow"] = flow
    # Xray streamSettings.network selects the server transport; sing-box network
    # filters proxied traffic, so copying "tcp" here would disable UDP.

    if security == "reality":
        result["tls"] = _convert_reality_tls(stream_settings)
    elif security == "tls":
        result["tls"] = _convert_standard_tls(stream_settings)
    if network == "grpc":
        result["transport"] = _convert_grpc_transport(stream_settings)
    elif network == "ws":
        result["transport"] = _convert_ws_transport(stream_settings)
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
    short_id = reality_settings.get("shortId", "")
    if not isinstance(short_id, str):
        raise ConfigPatchError("Invalid string field: realitySettings.shortId")
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
            "short_id": short_id,
        },
    }

    fingerprint = reality_settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": deepcopy(fingerprint)}
    if reality_settings.get("alpn"):
        tls["alpn"] = deepcopy(reality_settings["alpn"])

    return tls


def _convert_standard_tls(stream_settings: dict[str, Any]) -> dict[str, Any]:
    tls_settings = _optional_dict(stream_settings, "tlsSettings")
    for key in ("serverName", "fingerprint"):
        if key in tls_settings and not isinstance(tls_settings[key], str):
            raise ConfigPatchError("Invalid TLS string setting")
    if "alpn" in tls_settings:
        alpn = tls_settings["alpn"]
        if not isinstance(alpn, list) or any(
            not isinstance(item, str) or not item for item in alpn
        ):
            raise ConfigPatchError("Invalid TLS ALPN setting")
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
            raise ConfigPatchError("Unsupported TLS setting")
    tls: dict[str, Any] = {
        "enabled": True,
        "insecure": _optional_bool(tls_settings, "allowInsecure", "tlsSettings"),
    }
    for source_key, target_key in (("serverName", "server_name"), ("alpn", "alpn")):
        if tls_settings.get(source_key):
            tls[target_key] = deepcopy(tls_settings[source_key])
    fingerprint = tls_settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": deepcopy(fingerprint)}
    return tls


def _convert_ws_transport(stream_settings: dict[str, Any]) -> dict[str, Any]:
    settings = _optional_dict(stream_settings, "wsSettings")
    for key, value in settings.items():
        if key not in {"path", "headers", "host"} and value not in (
            None,
            "",
            False,
            0,
            [],
            {},
        ):
            raise WebSocketConfigError(
                "Конфиг содержит неподдерживаемые настройки WebSocket."
            )
    path = settings.get("path", "/")
    if not isinstance(path, str) or any(c in path for c in "\r\n\x00") or "#" in path:
        raise WebSocketConfigError("Некорректный путь WebSocket.")
    path = path or "/"
    if not path.startswith("/"):
        raise WebSocketConfigError("Некорректный путь WebSocket.")
    headers = deepcopy(_optional_dict(settings, "headers"))
    for key, value in headers.items():
        if (
            not isinstance(key, str)
            or not key
            or any(
                c
                not in "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                for c in key
            )
            or not isinstance(value, str)
            or any(c in value for c in "\r\n\x00")
        ):
            raise WebSocketConfigError("Некорректные заголовки WebSocket.")
    host = settings.get("host", "")
    if not isinstance(host, str) or any(c in host for c in "\r\n\x00"):
        raise WebSocketConfigError("Некорректный Host WebSocket.")
    host_headers = [key for key in headers if key.casefold() == "host"]
    if len(host_headers) > 1:
        raise WebSocketConfigError("В конфиге несколько заголовков Host WebSocket.")
    if host:
        for key in host_headers:
            del headers[key]
        headers["Host"] = host
    transport: dict[str, Any] = {"type": "ws", "path": path}
    if headers:
        transport["headers"] = headers

    # Xray's ?ed=N is a client option, not part of the server's HTTP path.
    base_path, separator, query = path.partition("?")
    if separator:
        parts = query.split("&")
        early_data = [part for part in parts if unquote(part.partition("=")[0]) == "ed"]
        if early_data:
            value = unquote(early_data[0].partition("=")[2])
            if (
                len(early_data) != 1
                or len(value) > 4
                or not value.isascii()
                or not value.isdecimal()
                or not 0 <= int(value) <= 8192
            ):
                raise WebSocketConfigError(
                    "Некорректный параметр early data WebSocket."
                )
            query = "&".join(part for part in parts if part not in early_data)
            transport["path"] = base_path + ("?" + query if query else "")
            if int(value):
                transport["max_early_data"] = int(value)
                transport["early_data_header_name"] = "Sec-WebSocket-Protocol"
    return transport


def _validate_plain_tcp_settings(stream_settings: dict[str, Any]) -> None:
    tcp_settings = _optional_dict(stream_settings, "tcpSettings")
    header = _optional_dict(tcp_settings, "header")
    if header and header != {"type": "none"}:
        raise ConfigPatchError("Unsupported TCP header")
    for key, value in tcp_settings.items():
        if key != "header" and value not in (None, "", False, 0, [], {}):
            raise ConfigPatchError("Unsupported TCP setting")


def _convert_grpc_transport(stream_settings: dict[str, Any]) -> dict[str, Any]:
    grpc_settings = _require_dict(stream_settings, "grpcSettings")
    transport: dict[str, Any] = {"type": "grpc"}
    service_name = grpc_settings.get("serviceName", "")
    if not isinstance(service_name, str):
        raise ConfigPatchError("Invalid string field: grpcSettings.serviceName")
    if service_name:
        transport["service_name"] = service_name

    # An Xray Reality server registers both Tun and TunMulti for a regular
    # service name. sing-box connects through Tun, so HAPP's client-side
    # multiMode flag can be omitted for that direct-server case. Custom
    # service paths can select different methods and must remain explicit.
    multi_mode = grpc_settings.get("multiMode", False)
    if type(multi_mode) is not bool:
        raise ConfigPatchError("Invalid boolean field: grpcSettings.multiMode")
    if multi_mode and (
        stream_settings.get("security") != "reality" or service_name.startswith("/")
    ):
        raise UnsupportedGrpcSettingError("multiMode")

    # sing-box has no separate HTTP/2 authority. Reject other non-default
    # values so the imported connection is not altered silently.
    for key, value in grpc_settings.items():
        if key in {"serviceName", "multiMode"}:
            continue
        if key == "authority" and not isinstance(value, str):
            raise ConfigPatchError("Invalid string field: grpcSettings.authority")
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
