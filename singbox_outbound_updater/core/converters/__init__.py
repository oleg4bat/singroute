"""Converters from exported client configs to sing-box-compatible data."""

from .outbound import normalize_outbound_to_singbox

__all__ = ["normalize_outbound_to_singbox"]
