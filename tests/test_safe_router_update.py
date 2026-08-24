from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json

import pytest

from singbox_outbound_updater.application.router_client import CommandResult
from singbox_outbound_updater.application.router_update import (
    RouterConfigChangedError,
    RouterUpdateError,
    apply_router_update,
    prepare_router_update,
)


CONFIG_PATH = "/etc/sing-box/config.json"
FIXED_NOW = datetime(2026, 6, 20, 12, 34, 56)
BACKUP_PATH = f"{CONFIG_PATH}.bak-20260620-123456"
TEMP_PATH = f"{CONFIG_PATH}.tmp-20260620-123456-000000"


def test_prepare_router_update_only_reads_and_returns_masked_preview():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})

    plan = prepare_router_update(_imported_config_text(), client)

    assert client.calls == [("read_text", CONFIG_PATH)]
    assert plan.config_path == CONFIG_PATH
    assert json.loads(plan.updated_config_text)["outbounds"][0]["server"] == "new.test"
    assert plan.preview["new_outbound"]["password"] == "***"
    assert "secret" not in json.dumps(plan.preview)


def test_safe_update_validates_installs_restarts_and_checks_status():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    client.calls.clear()

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is True
    assert result.backup_path == BACKUP_PATH
    assert result.backup_deleted is True
    assert result.rollback_success is None
    assert json.loads(client.files[CONFIG_PATH])["outbounds"][0]["server"] == "new.test"
    assert BACKUP_PATH not in client.files
    assert client.calls == [
        ("read_text", CONFIG_PATH),
        ("write_text", TEMP_PATH),
        ("run", f"sing-box check -c {TEMP_PATH}"),
        ("copy_file", CONFIG_PATH, BACKUP_PATH),
        ("run", f"mv -f {TEMP_PATH} {CONFIG_PATH}"),
        ("run", "/etc/init.d/sing-box restart"),
        ("run", "/etc/init.d/sing-box status"),
        ("run", f"rm -f {BACKUP_PATH}"),
    ]


def test_validation_failure_keeps_current_config_and_removes_temp():
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        command_results={
            f"sing-box check -c {TEMP_PATH}": CommandResult(
                command=f"sing-box check -c {TEMP_PATH}",
                exit_code=1,
                stderr="invalid config",
            )
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is False
    assert result.backup_path is None
    assert result.backup_deleted is False
    assert client.files[CONFIG_PATH] == _router_config_text()
    assert BACKUP_PATH not in client.files
    assert TEMP_PATH not in client.files
    assert ("run", f"rm -f {TEMP_PATH}") in client.calls
    assert not any(call[0] == "copy_file" for call in client.calls)
    assert not any(call == ("run", "/etc/init.d/sing-box restart") for call in client.calls)


def test_config_changed_after_preview_aborts_before_backup():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    client.files[CONFIG_PATH] = json.dumps({"outbounds": [{"type": "direct"}]})
    client.calls.clear()

    with pytest.raises(RouterConfigChangedError, match="изменился"):
        apply_router_update(plan, client, now=FIXED_NOW)

    assert client.calls == [("read_text", CONFIG_PATH)]
    assert BACKUP_PATH not in client.files


def test_restart_failure_restores_backup_and_restarts_old_config():
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

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is False
    assert result.rollback_success is True
    assert result.backup_deleted is True
    assert client.files[CONFIG_PATH] == _router_config_text()
    assert BACKUP_PATH not in client.files
    assert ("copy_file", BACKUP_PATH, CONFIG_PATH) in client.calls
    assert client.calls.count(("run", restart_command)) == 2
    assert ("run", f"rm -f {BACKUP_PATH}") in client.calls


def test_lost_restart_response_still_triggers_rollback():
    restart_command = "/etc/init.d/sing-box restart"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        command_errors={restart_command: [OSError("connection lost")]},
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is False
    assert result.rollback_success is True
    assert client.files[CONFIG_PATH] == _router_config_text()


def test_failed_rollback_keeps_backup_for_manual_recovery():
    restart_command = "/etc/init.d/sing-box restart"
    status_command = "/etc/init.d/sing-box status"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        sequenced_results={
            restart_command: [
                CommandResult(restart_command, 1, stderr="new config failed"),
                CommandResult(restart_command, 0, stdout="restart attempted"),
            ],
            status_command: [
                CommandResult(status_command, 1, stderr="not running"),
                CommandResult(status_command, 1, stderr="still not running"),
            ],
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is False
    assert result.rollback_success is False
    assert result.backup_deleted is False
    assert client.files[BACKUP_PATH] == _router_config_text()
    assert ("run", f"rm -f {BACKUP_PATH}") not in client.calls
    assert BACKUP_PATH in result.message


def test_backup_cleanup_failure_is_reported_without_hiding_successful_update():
    cleanup_command = f"rm -f {BACKUP_PATH}"
    client = SafeFakeRouterClient(
        files={CONFIG_PATH: _router_config_text()},
        command_results={
            cleanup_command: CommandResult(
                cleanup_command,
                1,
                stderr="read-only file system",
            )
        },
    )
    plan = prepare_router_update(_imported_config_text(), client)

    result = apply_router_update(plan, client, now=FIXED_NOW)

    assert result.success is True
    assert result.backup_deleted is False
    assert client.files[BACKUP_PATH] == _router_config_text()
    assert "удалить" in result.message


def test_service_name_cannot_inject_a_shell_command():
    client = SafeFakeRouterClient(files={CONFIG_PATH: _router_config_text()})
    plan = prepare_router_update(_imported_config_text(), client)
    client.calls.clear()

    with pytest.raises(RouterUpdateError, match="имя службы"):
        apply_router_update(plan, client, service_name="sing-box; reboot")

    assert client.calls == []


@dataclass
class SafeFakeRouterClient:
    files: dict[str, str]
    command_results: dict[str, CommandResult] = field(default_factory=dict)
    sequenced_results: dict[str, list[CommandResult]] = field(default_factory=dict)
    command_errors: dict[str, list[Exception]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def read_text(self, path: str) -> str:
        self.calls.append(("read_text", path))
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.calls.append(("write_text", path))
        self.files[path] = content

    def copy_file(self, source_path: str, target_path: str) -> None:
        self.calls.append(("copy_file", source_path, target_path))
        self.files[target_path] = self.files[source_path]

    def run(self, command: str) -> CommandResult:
        self.calls.append(("run", command))
        errors = self.command_errors.get(command)
        if errors:
            raise errors.pop(0)
        queued = self.sequenced_results.get(command)
        if queued:
            return queued.pop(0)

        result = self.command_results.get(command)
        if result is not None:
            return result

        if command.startswith("mv -f "):
            _, _, source, target = command.split(maxsplit=3)
            self.files[target] = self.files.pop(source)
        elif command.startswith("rm -f "):
            self.files.pop(command.removeprefix("rm -f "), None)

        return CommandResult(command=command, exit_code=0)


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
