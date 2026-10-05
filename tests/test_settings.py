from __future__ import annotations

from pathlib import Path

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

import singroute.infrastructure.settings as settings_module
from singroute.infrastructure.credentials import (
    LEGACY_SERVICE_NAME,
    SERVICE_NAME,
    CredentialStore,
    CredentialTarget,
)
from singroute.infrastructure.settings import (
    AppSettings,
    PortableSettingsStore,
)


def test_missing_ini_returns_openwrt_defaults(tmp_path: Path):
    settings = PortableSettingsStore(tmp_path / "app.ini").load()

    assert settings.host == "192.168.1.1"
    assert settings.port == 22
    assert settings.username == "root"
    assert settings.config_path == "/etc/sing-box/config.json"
    assert settings.service_name == "sing-box"
    assert settings.auth_mode == "auto"
    assert settings.remember_password is False
    assert settings.auto_connect is False
    assert settings.check_updates_on_startup is True
    assert settings.offer_ruantiblock_start is True


def test_settings_round_trip_to_portable_ini_without_password(tmp_path: Path):
    path = tmp_path / "SingRoute.ini"
    store = PortableSettingsStore(path)
    expected = AppSettings(
        host="openwrt.lan",
        port=2222,
        username="admin",
        config_path="/opt/sing-box/config.json",
        service_name="sing-box-custom",
        auth_mode="key",
        identity_file="D:/keys/router_ed25519",
        remember_password=True,
        auto_connect=True,
        check_updates_on_startup=False,
        offer_ruantiblock_start=False,
        last_import_directory="D:/configs",
        window_width=1100,
        window_height=800,
        trusted_host_keys={"openwrt.lan:2222": "ssh-ed25519 AAAATEST"},
    )

    store.save(expected)
    actual = store.load()

    assert actual == expected
    ini_text = path.read_text(encoding="utf-8")
    assert "password" not in ini_text.replace("remember_password", "")


def test_invalid_ini_values_fall_back_to_safe_defaults(tmp_path: Path):
    path = tmp_path / "app.ini"
    path.write_text(
        "[connection]\nport = 99999\nauth_mode = magic\n"
        "[application]\nwindow_width = 12\nwindow_height = nope\n",
        encoding="utf-8",
    )

    settings = PortableSettingsStore(path).load()

    assert settings.port == 22
    assert settings.auth_mode == "auto"
    assert settings.window_width == 960
    assert settings.window_height == 720


def test_malformed_ini_falls_back_to_safe_defaults(tmp_path: Path):
    path = tmp_path / "SingRoute.ini"
    path.write_text("[connection\nhost = broken", encoding="utf-8")

    settings = PortableSettingsStore(path).load()

    assert settings == AppSettings()


def test_credential_store_uses_endpoint_specific_windows_vault_key():
    backend = FakeKeyring()
    store = CredentialStore(backend)
    target = CredentialTarget("192.168.1.1", 22, "root")

    store.set_password(target, "router-password")

    assert backend.values[(SERVICE_NAME, "ssh://root@192.168.1.1:22")] == (
        "router-password"
    )
    assert store.get_password(target) == "router-password"

    store.delete_password(target)
    assert store.get_password(target) is None


def test_default_store_reads_legacy_ini_until_new_settings_are_saved(
    tmp_path: Path,
    monkeypatch,
):
    legacy_path = tmp_path / "singbox-outbound-updater.ini"
    legacy_path.write_text(
        "[connection]\nhost = legacy-router.lan\nport = 2222\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_module, "application_directory", lambda: tmp_path)

    store = PortableSettingsStore()
    loaded = store.load()

    assert loaded.host == "legacy-router.lan"
    assert loaded.port == 2222
    assert store.path == tmp_path / "SingRoute.ini"

    store.save(loaded)
    assert store.path.exists()


def test_credential_store_migrates_legacy_password_to_singroute_service():
    backend = FakeKeyring()
    store = CredentialStore(backend)
    target = CredentialTarget("openwrt.lan", 22, "root")
    backend.set_password(LEGACY_SERVICE_NAME, target.key, "legacy-password")

    assert store.get_password(target) == "legacy-password"
    assert backend.values[(SERVICE_NAME, target.key)] == "legacy-password"
    assert (LEGACY_SERVICE_NAME, target.key) not in backend.values


def test_credential_store_ignores_only_missing_password_on_delete():
    target = CredentialTarget("openwrt.lan", 22, "root")
    missing_backend = FailingDeleteKeyring(PasswordDeleteError("missing"))

    CredentialStore(missing_backend).delete_password(target)

    failing_backend = FailingDeleteKeyring(KeyringError("vault unavailable"))
    with pytest.raises(KeyringError, match="vault unavailable"):
        CredentialStore(failing_backend).delete_password(target)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class FailingDeleteKeyring(FakeKeyring):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def delete_password(self, service: str, username: str) -> None:
        raise self.error
