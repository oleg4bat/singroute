from __future__ import annotations

import paramiko
import pytest
import threading
import time

import singbox_outbound_updater.infrastructure.ssh_router as ssh_router
from singbox_outbound_updater.infrastructure.ssh_router import (
    HostKeyMismatchError,
    SshOperationCancelled,
    SshRouterClient,
    SshRouterError,
    UnknownHostKeyError,
)


def test_unknown_host_key_requires_explicit_confirmation():
    key = paramiko.RSAKey.generate(1024)
    policy = ssh_router._ExpectedHostKeyPolicy("192.168.1.1", 22, None)

    with pytest.raises(UnknownHostKeyError) as captured:
        policy.missing_host_key(FakePolicyClient(), "192.168.1.1", key)

    assert captured.value.info.fingerprint.startswith("SHA256:")
    assert captured.value.info.trust_token.startswith("ssh-rsa ")


def test_confirmed_host_key_is_accepted_and_changed_key_is_rejected():
    key = paramiko.RSAKey.generate(1024)
    info = ssh_router._host_key_info("openwrt.lan", 22, key)
    client = FakePolicyClient()
    policy = ssh_router._ExpectedHostKeyPolicy(
        "openwrt.lan", 22, info.trust_token
    )

    policy.missing_host_key(client, "openwrt.lan", key)
    assert client.host_keys.lookup("openwrt.lan") is not None

    changed_key = paramiko.RSAKey.generate(1024)
    with pytest.raises(HostKeyMismatchError):
        policy.missing_host_key(client, "openwrt.lan", changed_key)


def test_auto_auth_uses_agent_default_keys_and_ssh_config_identity(monkeypatch):
    fake = FakeSshClient()
    monkeypatch.setattr(ssh_router.paramiko, "SSHClient", lambda: fake)
    monkeypatch.setattr(
        ssh_router,
        "_load_ssh_config",
        lambda host: {"hostname": "192.168.1.1", "identityfile": ["~/router_key"]},
    )

    client = SshRouterClient(host="router", password="fallback", auth_mode="auto")
    client.connect()

    assert fake.connect_kwargs["hostname"] == "192.168.1.1"
    assert fake.connect_kwargs["allow_agent"] is True
    assert fake.connect_kwargs["look_for_keys"] is True
    assert fake.connect_kwargs["password"] == "fallback"
    assert fake.connect_kwargs["key_filename"][0].endswith("router_key")
    assert "fallback" not in repr(client)


def test_password_auth_disables_agent_and_key_discovery(monkeypatch):
    fake = FakeSshClient()
    monkeypatch.setattr(ssh_router.paramiko, "SSHClient", lambda: fake)
    monkeypatch.setattr(ssh_router, "_load_ssh_config", lambda host: {})

    SshRouterClient(
        host="192.168.1.1",
        password="secret",
        auth_mode="password",
    ).connect()

    assert fake.connect_kwargs["allow_agent"] is False
    assert fake.connect_kwargs["look_for_keys"] is False
    assert fake.connect_kwargs["key_filename"] is None
    assert fake.connect_kwargs["passphrase"] is None


def test_key_auth_does_not_fall_back_to_password(monkeypatch):
    fake = FakeSshClient()
    monkeypatch.setattr(ssh_router.paramiko, "SSHClient", lambda: fake)
    monkeypatch.setattr(ssh_router, "_load_ssh_config", lambda host: {})

    SshRouterClient(
        host="192.168.1.1",
        password="must-not-be-used",
        key_passphrase="key-passphrase",
        identity_file="router-key",
        auth_mode="key",
    ).connect()

    assert fake.connect_kwargs["password"] is None
    assert fake.connect_kwargs["passphrase"] == "key-passphrase"
    assert fake.connect_kwargs["key_filename"] == ["router-key"]


def test_read_text_uses_dropbear_compatible_cat_instead_of_sftp():
    transport = FakeExecClient(stdout=b'{"outbounds": []}\n')
    client = SshRouterClient(host="192.168.1.1")
    client._client = transport

    content = client.read_text("/etc/sing-box/config.json")

    assert content == '{"outbounds": []}\n'
    assert transport.command == "cat /etc/sing-box/config.json"


def test_write_text_streams_utf8_to_remote_cat_without_sftp():
    transport = FakeExecClient()
    client = SshRouterClient(host="192.168.1.1")
    client._client = transport

    client.write_text("/etc/sing-box/config new.json", '{"tag": "тест"}\n')

    assert transport.command == "umask 077; cat > '/etc/sing-box/config new.json'"
    assert transport.stdin.payload == '{"tag": "тест"}\n'.encode("utf-8")
    assert transport.stdin.shutdown_called is True


def test_cancelled_operation_stops_before_running_remote_command():
    cancelled = threading.Event()
    cancelled.set()
    client = SshRouterClient(host="192.168.1.1", cancel_event=cancelled)
    client._client = FakeExecClient()

    with pytest.raises(SshOperationCancelled):
        client.run("cat /etc/openwrt_release")


def test_remote_command_has_an_overall_response_timeout():
    transport = FakeExecClient(command_finishes=False)
    client = SshRouterClient(
        host="192.168.1.1",
        command_timeout=0.01,
    )
    client._client = transport

    with pytest.raises(SshRouterError, match="не ответила"):
        client.run("stuck-command")

    assert transport.channel.closed is True


def test_cat_accepts_dropbear_eof_when_server_omits_exit_status():
    transport = FakeExecClient(
        stdout=b'{"outbounds": []}\n',
        command_finishes=False,
        eof_received=True,
    )
    client = SshRouterClient(host="192.168.1.1", command_timeout=1)
    client._client = transport

    assert client.read_text("/etc/sing-box/config.json") == '{"outbounds": []}\n'
    assert transport.channel.closed is True


def test_client_close_does_not_block_gui_on_stuck_paramiko_cleanup():
    transport = BlockingCloseClient()
    client = SshRouterClient(host="192.168.1.1")
    client._client = transport

    started_at = time.monotonic()
    client.close()

    assert time.monotonic() - started_at < 0.1
    assert client._client is None
    assert transport.close_started.wait(timeout=1)
    transport.allow_close.set()


class FakePolicyClient:
    def __init__(self) -> None:
        self.host_keys = paramiko.HostKeys()

    def get_host_keys(self) -> paramiko.HostKeys:
        return self.host_keys


class FakeSshClient:
    def __init__(self) -> None:
        self.connect_kwargs = {}
        self.policy = None

    def load_system_host_keys(self) -> None:
        pass

    def load_host_keys(self, path: str) -> None:
        pass

    def set_missing_host_key_policy(self, policy: object) -> None:
        self.policy = policy

    def connect(self, **kwargs: object) -> None:
        self.connect_kwargs = kwargs

    def close(self) -> None:
        pass

    def get_transport(self) -> None:
        return None


class FakeExecClient:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        command_finishes: bool = True,
        eof_received: bool = False,
    ) -> None:
        self.command = ""
        self.stdin = FakeStdin()
        self.channel = FakeChannel(
            stdout,
            stderr,
            exit_code,
            command_finishes,
            eof_received,
        )

    def exec_command(self, command: str, timeout: float):
        self.command = command
        return (
            self.stdin,
            FakeOutput(self.channel),
            FakeOutput(self.channel),
        )

    def close(self) -> None:
        pass


class FakeStdin:
    def __init__(self) -> None:
        self.payload = b""
        self.shutdown_called = False
        self.channel = self

    def write(self, value: bytes) -> None:
        self.payload += value

    def flush(self) -> None:
        pass

    def shutdown_write(self) -> None:
        self.shutdown_called = True

    def close(self) -> None:
        pass


class FakeOutput:
    def __init__(self, channel: "FakeChannel") -> None:
        self.channel = channel


class FakeChannel:
    def __init__(
        self,
        stdout: bytes,
        stderr: bytes,
        exit_code: int,
        finishes: bool,
        eof_received: bool,
    ) -> None:
        self.stdout = bytearray(stdout)
        self.stderr = bytearray(stderr)
        self.exit_code = exit_code
        self.finishes = finishes
        self.eof_received = eof_received
        self.closed = False

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, size: int) -> bytes:
        value = bytes(self.stdout[:size])
        del self.stdout[:size]
        return value

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, size: int) -> bytes:
        value = bytes(self.stderr[:size])
        del self.stderr[:size]
        return value

    def exit_status_ready(self) -> bool:
        return self.finishes

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class BlockingCloseClient:
    def __init__(self) -> None:
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait(timeout=1)
