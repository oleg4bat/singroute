"""Small preview helpers for sing-box outbound data."""

from __future__ import annotations

import re
from typing import Any

from .outbound_order import order_outbound

SENSITIVE_FIELDS = {
    "api_key",
    "uuid",
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "private_key",
    "pre_shared_key",
    "preshared_key",
    "public_key",
    "short_id",
    "auth",
    "authorization",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
}
MASK = "***"


def summarize_outbound(outbound: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an outbound with sensitive fields masked recursively."""
    return order_outbound(_mask_sensitive_values(outbound))


def _mask_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: MASK if _is_sensitive_key(key) else _mask_sensitive_values(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_mask_sensitive_values(item) for item in value]

    return value


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    if normalized in SENSITIVE_FIELDS:
        return True
    parts = set(normalized.split("_"))
    return bool(parts & {"password", "passwd", "pwd", "passphrase", "secret", "token"})
