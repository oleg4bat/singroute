"""Small preview helpers for sing-box outbound data."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


SENSITIVE_FIELDS = {
    "uuid",
    "password",
    "private_key",
    "public_key",
    "short_id",
    "auth",
}
MASK = "***"


def summarize_outbound(outbound: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an outbound with sensitive fields masked recursively."""
    return _mask_sensitive_values(deepcopy(outbound))


def _mask_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: MASK if key in SENSITIVE_FIELDS else _mask_sensitive_values(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_mask_sensitive_values(item) for item in value]

    return value
