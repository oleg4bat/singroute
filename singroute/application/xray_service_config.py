"""Stage and restore OpenWrt's Xray file selection without editing examples."""

from __future__ import annotations

import hashlib
import shlex

from .router_client import RouterClient
from .router_connection import RouterEngine
from .router_update import (
    RouterUpdateError,
    RouterUpdatePlan,
    _acquire_update_lock,
    _build_guarded_install_command,
    _build_update_paths,
    _cleanup_operation_directory,
    _create_operation_directory,
    _reconcile_install_state,
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
        original = self.engine.xray_config_text
        self.issue = "Не удалось настроить загрузку основного конфига Xray."
        if original is None:
            return False
        _acquire_update_lock(self.client, self.paths, self.operation_id)
        self.locked = True
        if self.client.read_text("/etc/config/xray") != original:
            self.issue = "Настройки службы Xray изменились после подготовки превью."
            return False
        # Pending LuCI/uci edits must not be committed or discarded by SingRoute.
        pending = self.client.run("uci -q changes xray")
        if pending.exit_code != 0 or pending.stdout.strip():
            self.issue = "Не удалось подтвердить отсутствие несохранённых изменений UCI. Завершите изменение настроек Xray и повторите перенос."
            return False
        _create_operation_directory(self.client, self.paths.operation_directory)
        self.created = True
        stage = self.paths.operation_directory
        self.client.write_text(stage + "/xray", original)
        uci = f"uci -q -c {shlex.quote(stage)} -t {shlex.quote(stage + '/delta')}"
        command = " && ".join(
            [
                f"mkdir {shlex.quote(stage + '/delta')}",
                f"{uci} set {shlex.quote('xray.config.conffiles=' + self.engine.config_path)}",
                f"{uci} set xray.config.confdir=''",
                f"{uci} set xray.enabled.enabled=1",
                f"{uci} commit xray",
            ]
        )
        if _run_or_failure(self.client, command).exit_code != 0:
            self.issue = "Не удалось подготовить настройки службы Xray; действующие настройки не изменены."
            return False
        updated = self.client.read_text(stage + "/xray")
        pending = self.client.run("uci -q changes xray")
        if pending.exit_code != 0 or pending.stdout.strip():
            self.issue = "Во время подготовки появились несохранённые изменения UCI. Действующие настройки службы не заменены."
            return False
        self.client.write_text(self.paths.temporary_path, updated)
        self.plan = RouterUpdatePlan(
            "/etc/config/xray", original, updated, {}, original != updated
        )
        result = _run_or_failure(
            self.client, _build_guarded_install_command(self.plan, self.paths)
        )
        state, backup = _reconcile_install_state(self.plan, self.client, self.paths)
        self.uncertain = state not in {"original", "updated"}
        if result.exit_code != 0 and state != "updated":
            return False
        if not backup:
            self.uncertain = state != "original"
            return False
        # Confirm what init.d will consume, including any concurrent UCI edits.
        files = self.client.run("uci -q get xray.config.conffiles")
        directory = self.client.run("uci -q get xray.config.confdir")
        enabled = self.client.run("uci -q get xray.enabled.enabled")
        configured = (
            files.exit_code == enabled.exit_code == 0
            # uci set with an empty value deletes the optional confdir, so get
            # returns 1. Required options still prove the package is readable.
            and directory.exit_code in {0, 1}
            and files.stdout.split() == [self.engine.config_path]
            and not directory.stdout.strip()
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
                for path in (stage + "/xray", stage + "/delta/xray"):
                    _run_or_failure(self.client, f"rm -f {shlex.quote(path)}")
                _run_or_failure(self.client, f"rmdir {shlex.quote(stage + '/delta')}")
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
