from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json

import pytest

from singroute.application.router_client import CommandResult
from singroute.application.router_update import (
    CONFIG_CHANGED_EXIT,
    UPDATE_LOCKED_EXIT,
    RouterConfigChangedError,
    RouterUpdateError,
    _build_guarded_install_command,
    _build_update_paths,
    apply_router_update,
    prepare_router_update,
)


CONFIG_PATH = "/etc/sing-box/config.json"
FIXED_NOW = datetime(2026, 6, 20, 12, 34, 56)
OPERATION_ID = "a" * 32
PATHS = _build_update_paths(CONFIG_PATH, FIXED_NOW, OPERATION_ID)


def test_prepare_router_update_only_reads_and_returns_masked_preview():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})

    plan = prepare_router_update(_imported_config_text(), client)

    assert client.calls == [("read_text", CONFIG_PATH)]
    assert plan.config_path == CONFIG_PATH
    assert json.loads(plan.updated_config_text)["outbounds"][0]["server"] == "new.test"
    assert plan.preview["new_outbound"]["password"] == "***"
    assert "secret" not in json.dumps(plan.preview)


def test_safe_update_uses_private_directory_guarded_install_and_cleanup():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    install_command = _build_guarded_install_command(plan, PATHS)
    client.calls.clear()

    result = _apply(plan, client)

    assert result.success is True
    assert result.backup_path == PATHS.backup_path
    assert result.backup_deleted is True
    assert json.loads(client.files[CONFIG_PATH])["outbounds"][0]["server"] == "new.test"
    assert PATHS.backup_path not in client.files
    assert ("run", f"umask 077; mkdir {PATHS.operation_directory}") in client.calls
    assert ("write_text", PATHS.temporary_path) in client.calls
    assert ("run", install_command) in client.calls
    assert "sha256sum" in install_command
    assert PATHS.lock_path in install_command


def test_validation_failure_keeps_current_config_and_removes_private_files():
    validation_command = f"sing-box check -c {PATHS.temporary_path}"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        command_results={
            validation_command: CommandResult(validation_command, 1, stderr="invalid config")
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = _apply(plan, client)

    assert result.success is False
    assert result.backup_path is None
    assert client.files[CONFIG_PATH] == _router_config_text()
    assert PATHS.temporary_path not in client.files
    assert not any("sha256sum" in call[-1] for call in client.calls if call[0] == "run")


def test_config_changed_after_preview_aborts_before_private_directory():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    client.files[CONFIG_PATH] = json.dumps({"outbounds": [{"type": "direct"}]})
    client.calls.clear()

    with pytest.raises(RouterConfigChangedError, match="изменился"):
        _apply(plan, client)

    assert client.calls == [("read_text", CONFIG_PATH)]


def test_config_changed_during_validation_is_detected_by_guarded_install():
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        change_config_after_validation=True,
    )
    plan = prepare_router_update(_imported_config_text(), client)

    with pytest.raises(RouterConfigChangedError, match="во время проверки"):
        _apply(plan, client)

    assert json.loads(client.files[CONFIG_PATH])["outbounds"][0]["type"] == "direct"
    assert PATHS.backup_path not in client.files


def test_concurrent_update_lock_aborts_without_installing():
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        guarded_install_exit=UPDATE_LOCKED_EXIT,
    )
    plan = prepare_router_update(_imported_config_text(), client)

    with pytest.raises(RouterUpdateError, match="Другой экземпляр"):
        _apply(plan, client)

    assert client.files[CONFIG_PATH] == _router_config_text()


def test_lost_install_response_reconciles_updated_config_and_continues():
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        install_applies_then_loses_response=True,
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = _apply(plan, client)

    assert result.success is True
    assert json.loads(client.files[CONFIG_PATH])["outbounds"][0]["server"] == "new.test"
    assert ("read_text", CONFIG_PATH) in client.calls


def test_restart_failure_restores_backup_atomically_and_restarts_old_config():
    restart_command = "/etc/init.d/sing-box restart"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        sequenced_results={
            restart_command: [
                CommandResult(restart_command, 1, stderr="new config failed"),
                CommandResult(restart_command, 0, stdout="restored"),
            ]
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = _apply(plan, client)

    assert result.success is False
    assert result.rollback_success is True
    assert result.backup_deleted is True
    assert client.files[CONFIG_PATH] == _router_config_text()
    assert any(PATHS.restore_path in call[-1] for call in client.calls if call[0] == "run")


def test_failed_service_after_rollback_keeps_backup_for_manual_recovery():
    restart_command = "/etc/init.d/sing-box restart"
    status_command = "/etc/init.d/sing-box status"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        sequenced_results={
            restart_command: [
                CommandResult(restart_command, 1),
                CommandResult(restart_command, 0),
            ],
            status_command: [
                CommandResult(status_command, 1),
                CommandResult(status_command, 1),
            ],
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = _apply(plan, client)

    assert result.success is False
    assert result.rollback_success is False
    assert result.backup_deleted is False
    assert client.files[PATHS.backup_path] == _router_config_text()
    assert PATHS.backup_path in result.message


def test_service_name_cannot_inject_a_shell_command():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    client.calls.clear()

    with pytest.raises(RouterUpdateError, match="имя службы"):
        apply_router_update(plan, client, service_name="sing-box; reboot")

    assert client.calls == []


def test_config_path_rejects_control_characters_before_router_access():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})

    with pytest.raises(RouterUpdateError, match="путь"):
        prepare_router_update(_imported_config_text(), client, "/etc/config\nreboot")

    assert client.calls == []


def _apply(plan, client):
    return apply_router_update(
        plan,
        client,
        now=FIXED_NOW,
        operation_id=OPERATION_ID,
    )


@dataclass
class SafeFakeRouterClient:
    files: dict[str, str]
    command_results: dict[str, CommandResult] = field(default_factory=dict)
    sequenced_results: dict[str, list[CommandResult]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    change_config_after_validation: bool = False
    guarded_install_exit: int | None = None
    install_applies_then_loses_response: bool = False

    def read_text(self, path: str) -> str:
        self.calls.append(("read_text", path))
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.calls.append(("write_text", path))
        if path in self.files:
            raise OSError("exclusive create failed")
        self.files[path] = content

    def copy_file(self, source_path: str, target_path: str) -> None:
        self.calls.append(("copy_file", source_path, target_path))
        self.files[target_path] = self.files[source_path]

    def run(self, command: str) -> CommandResult:
        self.calls.append(("run", command))
        queued = self.sequenced_results.get(command)
        if queued:
            return queued.pop(0)
        result = self.command_results.get(command)
        if result is not None:
            return result

        if command == f"sing-box check -c {PATHS.temporary_path}":
            if self.change_config_after_validation:
                self.files[CONFIG_PATH] = json.dumps(
                    {"outbounds": [{"type": "direct"}]}
                )
            return CommandResult(command, 0)
        if "sha256sum" in command and PATHS.lock_path in command:
            if self.guarded_install_exit is not None:
                return CommandResult(command, self.guarded_install_exit)
            if self.files[CONFIG_PATH] != _router_config_text():
                self.files.pop(PATHS.backup_path, None)
                return CommandResult(command, CONFIG_CHANGED_EXIT)
            self.files[PATHS.backup_path] = self.files[CONFIG_PATH]
            self.files[CONFIG_PATH] = self.files.pop(PATHS.temporary_path)
            if self.install_applies_then_loses_response:
                return CommandResult(command, -1, stderr="connection lost")
            return CommandResult(command, 0)
        if command.startswith(f"cp -p {PATHS.backup_path} {PATHS.restore_path}"):
            self.files[PATHS.restore_path] = self.files[PATHS.backup_path]
            self.files[CONFIG_PATH] = self.files.pop(PATHS.restore_path)
            return CommandResult(command, 0)
        if command.startswith("test -f "):
            path = command.removeprefix("test -f ")
            return CommandResult(command, 0 if path in self.files else 1)
        if command.startswith("rm -f "):
            self.files.pop(command.removeprefix("rm -f "), None)
        return CommandResult(command, 0)


def _imported_config_text() -> str:
    return json.dumps(
        {
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "new.test",
                    "server_port": 443,
                    "password": "secret",
                }
            ]
        }
    )


def _router_config_text() -> str:
    return json.dumps(
        {
            "dns": {"servers": ["1.1.1.1"]},
            "outbounds": [
                {"type": "vless", "tag": "proxy", "server": "old.test"}
            ],
        }
    )
