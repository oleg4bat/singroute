"""Keep engine diagnostics useful without echoing configuration credentials."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

from .preview import MASK, _is_sensitive_key


def redact_config_output(output: str, *configs: Any) -> str:
    """Mask known secrets, including escaped values quoted by engine errors."""
    secrets: set[str] = set()

    def collect(value: Any, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(
                    item,
                    sensitive
                    or _is_sensitive_key(key)
                    or key in {"headers", "path", "shortIds"}
                    or (key == "encryption" and item != "none"),
                )
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.update(
                {
                    value,
                    json.dumps(value, ensure_ascii=False)[1:-1],
                    json.dumps(value, ensure_ascii=True)[1:-1],
                    quote(value, safe=""),
                    quote(value),
                    quote_plus(value, safe=""),
                }
            )

    for config in configs:
        collect(config)
    if not secrets:
        return output
    pattern = "|".join(
        re.escape(value) for value in sorted(secrets, key=len, reverse=True)
    )
    return re.sub(pattern, lambda _match: MASK, output)
