"""Domain errors for config validation and patching."""


class ConfigPatchError(ValueError):
    """Raised when a sing-box config cannot be patched safely."""


class ConfigParseError(ConfigPatchError):
    """Raised when a JSON config text cannot be parsed."""


class UnsupportedVlessTransportError(ConfigPatchError):
    """Raised when an Xray VLESS transport cannot be imported safely."""

    def __init__(self, network: object) -> None:
        super().__init__("Unsupported VLESS transport")
        normalized = network.casefold() if isinstance(network, str) else None
        self.network = (
            normalized
            if normalized in {"ws", "http", "httpupgrade", "quic", "xhttp"}
            else None
        )


class UnsupportedGrpcSettingError(ConfigPatchError):
    """Raised when an Xray gRPC option cannot be represented by sing-box."""

    def __init__(self, field: str) -> None:
        self.field = field if field in {"authority", "multiMode"} else None
        super().__init__(
            f"Unsupported gRPC setting: grpcSettings.{self.field or 'other'}"
        )
