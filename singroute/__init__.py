"""SingRoute application package."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("singroute")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
