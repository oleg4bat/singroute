"""sing-box outbound updater package."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("singbox-outbound-updater")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
