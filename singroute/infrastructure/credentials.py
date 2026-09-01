"""Password storage backed by the operating system credential vault."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from keyring.errors import PasswordDeleteError

SERVICE_NAME = "SingRoute"
LEGACY_SERVICE_NAME = "singbox-outbound-updater"


@dataclass(frozen=True)
class CredentialTarget:
    host: str
    port: int
    username: str

    @property
    def key(self) -> str:
        return f"ssh://{self.username}@{self.host}:{self.port}"


class CredentialStore:
    """Use keyring so Windows builds write to Windows Credential Manager."""

    def __init__(self, backend: Any | None = None) -> None:
        if backend is None:
            import keyring

            backend = keyring
        self._backend = backend

    def get_password(self, target: CredentialTarget) -> str | None:
        password = self._backend.get_password(SERVICE_NAME, target.key)
        if password is not None:
            return password

        legacy_password = self._backend.get_password(LEGACY_SERVICE_NAME, target.key)
        if legacy_password is None:
            return None

        try:
            self._backend.set_password(SERVICE_NAME, target.key, legacy_password)
            self._delete_password(LEGACY_SERVICE_NAME, target)
        except Exception:
            # Reading a saved password must still work if migration is unavailable.
            pass
        return legacy_password

    def set_password(self, target: CredentialTarget, password: str) -> None:
        if not password:
            self.delete_password(target)
            return
        self._backend.set_password(SERVICE_NAME, target.key, password)

    def delete_password(self, target: CredentialTarget) -> None:
        self._delete_password(SERVICE_NAME, target)
        self._delete_password(LEGACY_SERVICE_NAME, target)

    def _delete_password(self, service_name: str, target: CredentialTarget) -> None:
        # Deleting an already absent credential is idempotent.
        with suppress(PasswordDeleteError):
            self._backend.delete_password(service_name, target.key)
