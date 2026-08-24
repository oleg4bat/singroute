"""Domain errors for config validation and patching."""


class ConfigPatchError(ValueError):
    """Raised when a sing-box config cannot be patched safely."""


class ConfigParseError(ConfigPatchError):
    """Raised when a JSON config text cannot be parsed."""
