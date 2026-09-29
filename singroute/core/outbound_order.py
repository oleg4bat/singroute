"""Stable, human-readable field order for sing-box proxy outbounds."""

from __future__ import annotations

from typing import Any

_COMMON_FIELDS = ("type", "tag", "server", "server_port")
_OUTBOUND_FIELDS = {
    "vless": (
        *_COMMON_FIELDS,
        "uuid",
        "flow",
        "network",
        "tls",
        "packet_encoding",
        "multiplex",
        "transport",
    ),
    "trojan": (*_COMMON_FIELDS, "password", "network", "tls", "multiplex", "transport"),
    "hysteria2": (
        *_COMMON_FIELDS,
        "server_ports",
        "hop_interval",
        "hop_interval_max",
        "up_mbps",
        "down_mbps",
        "obfs",
        "password",
        "network",
        "tls",
    ),
}
_NESTED_FIELDS = {
    ("tls",): ("enabled", "server_name", "insecure", "alpn", "reality", "utls"),
    ("tls", "reality"): ("enabled", "public_key", "short_id"),
    ("tls", "utls"): ("enabled", "fingerprint"),
    ("transport",): ("type", "service_name"),
    ("obfs",): ("type", "password"),
}


def order_outbound(outbound: dict[str, Any]) -> dict[str, Any]:
    """Return the same outbound values in a stable display and serialization order."""
    outbound_type = outbound.get("type")
    preferred = (
        _OUTBOUND_FIELDS.get(outbound_type, _COMMON_FIELDS)
        if isinstance(outbound_type, str)
        else _COMMON_FIELDS
    )
    return _order_mapping(outbound, preferred, ())


def _order_mapping(
    mapping: dict[str, Any], preferred: tuple[str, ...], path: tuple[str, ...]
) -> dict[str, Any]:
    remaining = sorted(key for key in mapping if key not in preferred)
    return {
        key: _order_value(mapping[key], (*path, key))
        for key in (*preferred, *remaining)
        if key in mapping
    }


def _order_value(value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return _order_mapping(value, _NESTED_FIELDS.get(path, ()), path)
    if isinstance(value, list):
        return [_order_value(item, path) for item in value]
    return value
