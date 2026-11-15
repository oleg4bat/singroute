"""Enable Xray transactionally without changing the service's config path."""

from __future__ import annotations

import hashlib
import shlex

from .router_client import RouterClient, run_commands
from .router_connection import RouterEngine
from .router_update import (
    RouterUpdateError,
    RouterUpdatePlan,
    _acquire_update_lock,
    _build_guarded_install_command,
    _build_update_paths,
    _cleanup_operation_directory,
    _create_operation_directory,
    _release_update_lock,
    _run_or_failure,
)


class XrayServiceConfig:
    def __init__(self, client: RouterClient, engine: RouterEngine, operation_id: str):
        self.client, self.engine, self.operation_id = client, engine, operation_id
        self.paths = _build_update_paths("/etc/config/xray", None, operation_id)
        self.plan: RouterUpdatePlan | None = None
        self.locked = False
        self.created = False
        self.uncertain = False
        self.issue = ""

    def install(self) -> bool:
        """Generate UCI changes in a private copy, then compare and replace."""
        self.issue = "Не удалось включить Xray в настройках службы OpenWrt."
        _acquire_update_lock(self.client, self.paths, self.operation_id)
        self.locked = True
        # Pending LuCI/uci edits must not be committed or discarded by SingRoute.
        snapshot = run_commands(
            self.client,
            {
                "config": "cat /etc/config/xray",
                "pending": "uci -q changes xray",
            },
            "Подготавливаю включение Xray",
        )
        pending = snapshot["pending"]
        if (
            snapshot["config"].exit_code != 0
            or pending.exit_code != 0
            or pending.stdout.strip()
        ):
            self.issue = "Не удалось подтвердить отсутствие несохранённых изменений UCI. Завершите изменение настроек Xray и повторите перенос."
            return False
        original = snapshot["config"].stdout
        _create_operation_directory(self.client, self.paths.operation_directory)
        self.created = True
        stage = self.paths.operation_directory
        self.client.write_text(stage + "/xray", original)
        uci = f"uci -q -c {shlex.quote(stage)} -t {shlex.quote(stage + '/delta')}"
        command = " && ".join(
            [
                f"mkdir {shlex.quote(stage + '/delta')}",
                f"{uci} set xray.enabled.enabled=1",
                f"{uci} commit xray",
            ]
        )
        if _run_or_failure(self.client, command).exit_code != 0:
            self.issue = "Не удалось подготовить настройки службы Xray; действующие настройки не изменены."
            return False
        prepared = run_commands(
            self.client,
            {
                "config": f"cat {shlex.quote(stage + '/xray')}",
                "pending": "uci -q changes xray",
            },
            "Читаю подготовленные настройки Xray",
        )
        pending = prepared["pending"]
        if (
            prepared["config"].exit_code != 0
            or pending.exit_code != 0
            or pending.stdout.strip()
        ):
            self.issue = "Во время подготовки появились несохранённые изменения UCI. Действующие настройки службы не заменены."
            return False
        updated = prepared["config"].stdout
        self.client.write_text(self.paths.temporary_path, updated)
        self.plan = RouterUpdatePlan(
            "/etc/config/xray", original, updated, {}, original != updated
        )
        result = _run_or_failure(
            self.client, _build_guarded_install_command(self.plan, self.paths)
        )
        installed = run_commands(
            self.client,
            {
                "config": "cat /etc/config/xray",
                "backup": f"test -f {shlex.quote(self.paths.backup_path)}",
                "enabled": "uci -q get xray.enabled.enabled",
            },
            "Проверяю включение Xray",
        )
        current = installed["config"]
        state = (
            "updated"
            if current.exit_code == 0 and current.stdout == updated
            else "original"
            if current.exit_code == 0 and current.stdout == original
            else "unknown"
        )
        backup = installed["backup"].exit_code == 0
        self.uncertain = state not in {"original", "updated"}
        if result.exit_code != 0 and state != "updated":
            return False
        if not backup:
            self.uncertain = state != "original"
            return False
        enabled = installed["enabled"]
        configured = (
            state == "updated"
            and enabled.exit_code == 0
            and enabled.stdout.strip() == "1"
        )
        if configured:
            self.issue = ""
        return configured

    def restore(self) -> bool:
        if self.uncertain:
            return False
        if self.plan is None:
            return True  # Staging does not touch the live UCI file.
        try:
            current = self.client.read_text(self.plan.config_path)
            if current == self.plan.original_config_text:
                return True
            if current != self.plan.updated_config_text:
                self.uncertain = True
                return False
            digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
            command = (
                f"actual=$(sha256sum {shlex.quote(self.plan.config_path)}) && "
                "actual=${actual%% *} && "
                f'[ "$actual" = {shlex.quote(digest)} ] && '
                f"cp -p {shlex.quote(self.paths.backup_path)} "
                f"{shlex.quote(self.paths.restore_path)} && "
                f"mv -f {shlex.quote(self.paths.restore_path)} "
                f"{shlex.quote(self.plan.config_path)}"
            )
            _run_or_failure(self.client, command)
            return (
                self.client.read_text(self.plan.config_path)
                == self.plan.original_config_text
            )
        except Exception:
            self.uncertain = True
            return False

    def close(self, safe: bool) -> str:
        """Keep recovery files if rollback could not be confirmed."""
        message = self.issue
        if self.created:
            if safe and not self.uncertain:
                stage = self.paths.operation_directory
                _run_or_failure(
                    self.client,
                    (
                        f"rm -f {shlex.quote(stage + '/xray')} {shlex.quote(stage + '/delta/xray')} "
                        f"&& rmdir {shlex.quote(stage + '/delta')}"
                    ),
                )
                cleanup = _cleanup_operation_directory(self.client, self.paths)
                if not cleanup.directory_deleted:
                    message += f" Остались служебные файлы: {stage}."
            else:
                message += f" Настройки службы Xray требуют проверки. Резервная копия: {self.paths.backup_path}."
        if self.locked:
            try:
                _release_update_lock(self.client, self.paths, self.operation_id)
            except RouterUpdateError as exc:
                message += " " + str(exc)
            self.locked = False
        return message.strip()
