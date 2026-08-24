"""Portable INI settings stored next to the executable."""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
import os
import sys

from singroute.application.router_update import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SERVICE_NAME,
)


SETTINGS_FILENAME = "SingRoute.ini"
LEGACY_SETTINGS_FILENAME = "singbox-outbound-updater.ini"
VALID_AUTH_MODES = {"auto", "key", "password"}


@dataclass
class AppSettings:
    host: str = "192.168.1.1"
    port: int = 22
    username: str = "root"
    config_path: str = DEFAULT_CONFIG_PATH
    service_name: str = DEFAULT_SERVICE_NAME
    auth_mode: str = "auto"
    identity_file: str = ""
    remember_password: bool = False
    auto_connect: bool = False
    check_updates_on_startup: bool = True
    last_import_directory: str = ""
    window_width: int = 960
    window_height: int = 720
    trusted_host_keys: dict[str, str] = field(default_factory=dict)

    def host_key_id(self) -> str:
        return f"{self.host.strip()}:{self.port}"


class PortableSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            directory = application_directory()
            self.path = directory / SETTINGS_FILENAME
            self.legacy_path: Path | None = directory / LEGACY_SETTINGS_FILENAME
        else:
            self.path = path
            self.legacy_path = None

    def load(self) -> AppSettings:
        settings = AppSettings()
        source_path = self.path
        if not source_path.exists() and self.legacy_path is not None:
            source_path = self.legacy_path
        if not source_path.exists():
            return settings

        parser = _new_parser()
        try:
            parser.read(source_path, encoding="utf-8")
        except (OSError, UnicodeError):
            return settings

        connection = parser["connection"] if parser.has_section("connection") else {}
        application = parser["application"] if parser.has_section("application") else {}

        settings.host = _text(connection, "host", settings.host)
        settings.port = _bounded_int(connection, "port", settings.port, 1, 65535)
        settings.username = _text(connection, "username", settings.username)
        settings.config_path = _text(
            connection, "config_path", settings.config_path
        )
        settings.service_name = _text(
            connection, "service_name", settings.service_name
        )
        auth_mode = _text(connection, "auth_mode", settings.auth_mode)
        settings.auth_mode = auth_mode if auth_mode in VALID_AUTH_MODES else "auto"
        settings.identity_file = _text(connection, "identity_file", "")
        settings.remember_password = _boolean(
            connection, "remember_password", False
        )
        settings.auto_connect = _boolean(connection, "auto_connect", False)
        settings.check_updates_on_startup = _boolean(
            application, "check_updates_on_startup", True
        )

        settings.last_import_directory = _text(
            application, "last_import_directory", ""
        )
        settings.window_width = _bounded_int(
            application, "window_width", settings.window_width, 640, 4096
        )
        settings.window_height = _bounded_int(
            application, "window_height", settings.window_height, 480, 2160
        )

        if parser.has_section("trusted_host_keys"):
            settings.trusted_host_keys = dict(parser.items("trusted_host_keys"))
        return settings

    def save(self, settings: AppSettings) -> None:
        parser = _new_parser()
        parser["connection"] = {
            "host": settings.host.strip(),
            "port": str(settings.port),
            "username": settings.username.strip(),
            "config_path": settings.config_path.strip(),
            "service_name": settings.service_name.strip(),
            "auth_mode": settings.auth_mode,
            "identity_file": settings.identity_file.strip(),
            "remember_password": str(settings.remember_password).lower(),
            "auto_connect": str(settings.auto_connect).lower(),
        }
        parser["application"] = {
            "check_updates_on_startup": str(settings.check_updates_on_startup).lower(),
            "last_import_directory": settings.last_import_directory,
            "window_width": str(settings.window_width),
            "window_height": str(settings.window_height),
        }
        parser["trusted_host_keys"] = dict(settings.trusted_host_keys)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
                parser.write(stream)
            os.replace(temporary_path, self.path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError(
                f"Не удалось сохранить настройки рядом с программой: {self.path}: {error}"
            ) from error


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _new_parser() -> ConfigParser:
    parser = ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str
    return parser


def _text(section: object, key: str, default: str) -> str:
    try:
        value = section.get(key, default)  # type: ignore[attr-defined]
    except Exception:
        return default
    value = str(value).strip()
    return value or default


def _bounded_int(
    section: object,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(section.get(key, str(default)))  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return default
    return value if minimum <= value <= maximum else default


def _boolean(section: object, key: str, default: bool) -> bool:
    try:
        value = str(section.get(key, str(default))).strip().lower()  # type: ignore[attr-defined]
    except Exception:
        return default
    if value in {"1", "yes", "true", "on"}:
        return True
    if value in {"0", "no", "false", "off"}:
        return False
    return default
