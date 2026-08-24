"""Password storage backed by the operating system credential vault."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SERVICE_NAME = "singbox-outbound-updater"


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
        return self._backend.get_password(SERVICE_NAME, target.key)

    def set_password(self, target: CredentialTarget, password: str) -> None:
        if not password:
            self.delete_password(target)
            return
        self._backend.set_password(SERVICE_NAME, target.key, password)

    def delete_password(self, target: CredentialTarget) -> None:
        try:
            self._backend.delete_password(SERVICE_NAME, target.key)
        except Exception as error:
            if error.__class__.__name__ not in {"PasswordDeleteError", "KeyringError"}:
                raise
