"""Paramiko-based SSH client bundled into the portable Windows executable."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import hmac
from pathlib import Path
import shlex
import threading
import time
from typing import Any, Callable

import paramiko

from singbox_outbound_updater.application.router_client import CommandResult


class SshRouterError(RuntimeError):
    """Raised when an SSH connection or remote operation fails."""


class SshOperationCancelled(SshRouterError):
    """Raised when the user cancels an active SSH operation."""


@dataclass(frozen=True)
class HostKeyInfo:
    host: str
    port: int
    algorithm: str
    public_key: str
    fingerprint: str

    @property
    def trust_token(self) -> str:
        return f"{self.algorithm} {self.public_key}"


class UnknownHostKeyError(SshRouterError):
    def __init__(self, info: HostKeyInfo) -> None:
        self.info = info
        super().__init__(
            f"Ключ роутера {info.host}:{info.port} ещё не подтверждён "
            f"({info.fingerprint})."
        )


class HostKeyMismatchError(SshRouterError):
    def __init__(self, info: HostKeyInfo) -> None:
        self.info = info
        super().__init__(
            f"SSH-ключ роутера {info.host}:{info.port} изменился. "
            f"Полученный fingerprint: {info.fingerprint}."
        )


@dataclass
class SshRouterClient:
    host: str
    user: str = "root"
    port: int = 22
    identity_file: str | None = None
    password: str | None = field(default=None, repr=False)
    key_passphrase: str | None = field(default=None, repr=False)
    auth_mode: str = "auto"
    trusted_host_key: str | None = field(default=None, repr=False)
    timeout: float = 10.0
    command_timeout: float = 15.0
    keepalive_interval: int = 30
    cancel_event: threading.Event | None = field(default=None, repr=False)
    progress_callback: Callable[[str], None] | None = field(default=None, repr=False)
    _client: paramiko.SSHClient | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def connect(self) -> None:
        self._check_cancelled()
        if self._client is not None:
            return
        if self.auth_mode not in {"auto", "key", "password"}:
            raise SshRouterError(f"Неизвестный режим SSH-аутентификации: {self.auth_mode}")

        ssh_config = _load_ssh_config(self.host)
        resolved_host = str(ssh_config.get("hostname", self.host))
        identity_files = _identity_files(self.identity_file, ssh_config)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if known_hosts.is_file():
            try:
                client.load_host_keys(str(known_hosts))
            except (OSError, paramiko.SSHException):
                pass
        client.set_missing_host_key_policy(
            _ExpectedHostKeyPolicy(
                host=self.host,
                port=self.port,
                expected_token=self.trusted_host_key,
            )
        )

        use_keys = self.auth_mode in {"auto", "key"}
        use_password = self.auth_mode in {"auto", "password"}
        self._report(f"SSH: подключение к {self.user}@{self.host}:{self.port}")
        self._client = client
        try:
            client.connect(
                hostname=resolved_host,
                port=self.port,
                username=self.user,
                password=(self.password or None) if use_password else None,
                passphrase=(self.key_passphrase or None) if use_keys else None,
                key_filename=(identity_files or None) if use_keys else None,
                allow_agent=use_keys,
                look_for_keys=use_keys,
                timeout=self.timeout,
                auth_timeout=self.timeout,
                banner_timeout=self.timeout,
            )
        except (UnknownHostKeyError, HostKeyMismatchError):
            self.close()
            raise
        except paramiko.BadHostKeyException as error:
            self.close()
            raise HostKeyMismatchError(
                _host_key_info(self.host, self.port, error.key)
            ) from error
        except Exception as error:
            self.close()
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise SshOperationCancelled("Операция отменена пользователем.") from error
            raise SshRouterError(
                f"Не удалось подключиться к {self.user}@{self.host}:{self.port}: {error}"
            ) from error

        try:
            self._check_cancelled()
        except SshOperationCancelled:
            self.close()
            raise
        transport = client.get_transport()
        if transport is not None and self.keepalive_interval > 0:
            transport.set_keepalive(self.keepalive_interval)
        self._report("SSH: соединение установлено")

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        threading.Thread(
            target=_close_paramiko_client,
            args=(client,),
            name="ssh-client-close",
            daemon=True,
        ).start()

    def read_text(self, path: str) -> str:
        result = self.run(f"cat {shlex.quote(path)}")
        if result.exit_code not in {0, -1}:
            message = result.stderr.strip() or result.stdout.strip() or "без описания"
            raise SshRouterError(f"Не удалось прочитать {path}: {message}")
        if result.exit_code == -1 and result.stderr.strip():
            raise SshRouterError(f"Не удалось прочитать {path}: {result.stderr.strip()}")
        return result.stdout

    def write_text(self, path: str, content: str) -> None:
        result = self._execute(
            f"umask 077; cat > {shlex.quote(path)}",
            input_text=content,
        )
        if result.exit_code not in {0, -1}:
            message = result.stderr.strip() or result.stdout.strip() or "без описания"
            raise SshRouterError(f"Не удалось записать {path}: {message}")
        if result.exit_code == -1 and result.stderr.strip():
            raise SshRouterError(f"Не удалось записать {path}: {result.stderr.strip()}")

    def copy_file(self, source_path: str, target_path: str) -> None:
        result = self.run(
            f"cp -p {shlex.quote(source_path)} {shlex.quote(target_path)}"
        )
        self._raise_for_failure(result)

    def run(self, command: str) -> CommandResult:
        return self._execute(command)

    def _execute(
        self,
        command: str,
        input_text: str | None = None,
    ) -> CommandResult:
        self._check_cancelled()
        client = self._connected_client()
        self._report(f"SSH: {command}")
        try:
            stdin, stdout, stderr = client.exec_command(
                command,
                timeout=self.timeout,
            )
            channel = stdout.channel
            if input_text is not None:
                stdin.write(input_text.encode("utf-8"))
                stdin.flush()
                stdin.channel.shutdown_write()
            stdin.close()
            stdout_bytes, stderr_bytes, exit_code = self._read_command_output(
                channel
            )
        except SshOperationCancelled:
            raise
        except Exception as error:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise SshOperationCancelled("Операция отменена пользователем.") from error
            raise SshRouterError(f"SSH-команда не выполнена: {command}: {error}") from error
        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=_decode_output(stdout_bytes),
            stderr=_decode_output(stderr_bytes),
        )
        if exit_code == -1:
            self._report("SSH: команда завершила вывод без exit-status")
        else:
            self._report(f"SSH: команда завершена, код {exit_code}")
        return result

    def _read_command_output(self, channel: Any) -> tuple[bytes, bytes, int]:
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + self.command_timeout
        eof_seen_at: float | None = None

        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                channel.close()
                raise SshOperationCancelled("Операция отменена пользователем.")

            while channel.recv_ready():
                stdout.extend(channel.recv(32768))
            while channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(32768))

            if channel.exit_status_ready():
                while channel.recv_ready():
                    stdout.extend(channel.recv(32768))
                while channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(32768))
                return bytes(stdout), bytes(stderr), channel.recv_exit_status()

            if getattr(channel, "eof_received", False):
                if eof_seen_at is None:
                    eof_seen_at = time.monotonic()
                elif time.monotonic() - eof_seen_at >= 0.25:
                    channel.close()
                    return bytes(stdout), bytes(stderr), -1
            else:
                eof_seen_at = None

            if time.monotonic() >= deadline:
                channel.close()
                raise SshRouterError(
                    f"SSH-команда не ответила за {self.command_timeout:g} секунд."
                )
            time.sleep(0.05)

    def _connected_client(self) -> paramiko.SSHClient:
        self.connect()
        if self._client is None:
            raise SshRouterError("SSH-соединение не установлено.")
        return self._client

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SshOperationCancelled("Операция отменена пользователем.")

    def _report(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    @staticmethod
    def _raise_for_failure(result: CommandResult) -> None:
        if result.exit_code != 0:
            message = result.stderr.strip() or result.stdout.strip() or "без описания"
            raise SshRouterError(
                f"SSH-команда завершилась с кодом {result.exit_code}: "
                f"{result.command}: {message}"
            )


class _ExpectedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, host: str, port: int, expected_token: str | None) -> None:
        self.host = host
        self.port = port
        self.expected_token = expected_token

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        info = _host_key_info(self.host, self.port, key)
        if self.expected_token is None:
            raise UnknownHostKeyError(info)
        if not hmac.compare_digest(self.expected_token, info.trust_token):
            raise HostKeyMismatchError(info)
        client.get_host_keys().add(hostname, key.get_name(), key)


def _host_key_info(host: str, port: int, key: paramiko.PKey) -> HostKeyInfo:
    digest = hashlib.sha256(key.asbytes()).digest()
    fingerprint = base64.b64encode(digest).decode("ascii").rstrip("=")
    return HostKeyInfo(
        host=host,
        port=port,
        algorithm=key.get_name(),
        public_key=key.get_base64(),
        fingerprint=f"SHA256:{fingerprint}",
    )


def _load_ssh_config(host: str) -> dict[str, Any]:
    config_path = Path.home() / ".ssh" / "config"
    if not config_path.is_file():
        return {}
    try:
        config = paramiko.SSHConfig()
        with config_path.open("r", encoding="utf-8") as stream:
            config.parse(stream)
        return dict(config.lookup(host))
    except (OSError, UnicodeError, paramiko.SSHException):
        return {}


def _identity_files(
    explicit_identity_file: str | None,
    ssh_config: dict[str, Any],
) -> list[str]:
    if explicit_identity_file:
        return [str(Path(explicit_identity_file).expanduser())]
    configured = ssh_config.get("identityfile", [])
    if isinstance(configured, str):
        configured = [configured]
    return [str(Path(path).expanduser()) for path in configured]


def _decode_output(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _close_paramiko_client(client: paramiko.SSHClient) -> None:
    try:
        client.close()
    except Exception:
        pass
