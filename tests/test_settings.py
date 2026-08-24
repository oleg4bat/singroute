from __future__ import annotations

from pathlib import Path

from singbox_outbound_updater.infrastructure.credentials import (
    CredentialStore,
    CredentialTarget,
    SERVICE_NAME,
)
from singbox_outbound_updater.infrastructure.settings import (
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


def test_settings_round_trip_to_portable_ini_without_password(tmp_path: Path):
    path = tmp_path / "singbox-outbound-updater.ini"
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


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)
