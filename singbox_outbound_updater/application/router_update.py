"""Application use case for updating a router sing-box config."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
import shlex
from typing import Any

from singbox_outbound_updater.application.operation import (
    prepare_config_update,
    summarize_router_outbound,
)
from singbox_outbound_updater.application.router_client import (
    CommandResult,
    RouterClient,
)


DEFAULT_CONFIG_PATH = "/etc/sing-box/config.json"
DEFAULT_SERVICE_NAME = "sing-box"
DEFAULT_SING_BOX_COMMAND = "sing-box"


class RouterUpdateError(RuntimeError):
    """Raised when a router update cannot be completed safely."""


class RouterConfigChangedError(RouterUpdateError):
    """Raised when the router config changed after the preview was prepared."""


@dataclass(frozen=True)
class RouterUpdatePlan:
    """Prepared update shown to the user before any router mutation."""

    config_path: str
    original_config_text: str
    updated_config_text: str
    preview: dict[str, Any]


@dataclass(frozen=True)
class RouterUpdateResult:
    """User-facing outcome of the guarded OpenWrt update workflow."""

    success: bool
    backup_path: str | None
    validation_result: CommandResult
    rollback_success: bool | None = None
    backup_deleted: bool = False
    message: str = ""


def prepare_router_update(
    imported_config_content: str,
    router_client: RouterClient,
    config_path: str = DEFAULT_CONFIG_PATH,
) -> RouterUpdatePlan:
    """Read the router and prepare a preview without changing remote state."""
    original_config_text = router_client.read_text(config_path)
    update = prepare_config_update(
        imported_config_content,
        original_config_text,
    )
    return RouterUpdatePlan(
        config_path=config_path,
        original_config_text=original_config_text,
        updated_config_text=_dump_config_text(update.updated_config),
        preview=update.preview,
    )


def read_router_outbound_summary(
    router_client: RouterClient,
    config_path: str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Read and mask the outbound that the application would replace."""
    return summarize_router_outbound(router_client.read_text(config_path))


def apply_router_update(
    plan: RouterUpdatePlan,
    router_client: RouterClient,
    service_name: str = DEFAULT_SERVICE_NAME,
    sing_box_command: str = DEFAULT_SING_BOX_COMMAND,
    now: datetime | None = None,
) -> RouterUpdateResult:
    """Validate, atomically install, restart, and roll back on OpenWrt failures."""
    _validate_service_name(service_name)
    current_config_text = router_client.read_text(plan.config_path)
    if current_config_text != plan.original_config_text:
        raise RouterConfigChangedError(
            "Конфиг роутера изменился после подготовки превью. "
            "Обновите превью и повторите операцию."
        )

    backup_path = build_backup_path(plan.config_path, now)
    temporary_path = build_temporary_path(plan.config_path, now)
    backup_created = False
    temporary_pending = True

    try:
        router_client.write_text(temporary_path, plan.updated_config_text)
        validation_result = router_client.run(
            f"{shlex.quote(sing_box_command)} check -c {shlex.quote(temporary_path)}"
        )
        if validation_result.exit_code != 0:
            _remove_temporary_file(router_client, temporary_path)
            temporary_pending = False
            return RouterUpdateResult(
                success=False,
                backup_path=None,
                validation_result=validation_result,
                message="Новый конфиг не прошёл проверку sing-box; текущий конфиг не изменён.",
            )

        router_client.copy_file(plan.config_path, backup_path)
        backup_created = True
        install_result = router_client.run(
            f"mv -f {shlex.quote(temporary_path)} {shlex.quote(plan.config_path)}"
        )
        if install_result.exit_code != 0:
            raise RouterUpdateError(
                _command_failure_message("Не удалось установить новый конфиг", install_result)
            )
        temporary_pending = False

        service_command = shlex.quote(f"/etc/init.d/{service_name}")
        restart_result = _run_or_failure(router_client, f"{service_command} restart")
        status_result = _run_or_failure(router_client, f"{service_command} status")
        if restart_result.exit_code == 0 and status_result.exit_code == 0:
            backup_cleanup_result = _remove_file(router_client, backup_path)
            backup_deleted = backup_cleanup_result.exit_code == 0
            if backup_deleted:
                message = (
                    "Конфиг обновлён, sing-box успешно перезапущен; "
                    "временная резервная копия удалена."
                )
            else:
                message = (
                    "Конфиг обновлён и sing-box успешно перезапущен, но удалить "
                    "временную резервную копию не удалось."
                )
            return RouterUpdateResult(
                success=True,
                backup_path=backup_path,
                validation_result=validation_result,
                backup_deleted=backup_deleted,
                message=message,
            )

        return _rollback_after_service_failure(
            plan=plan,
            router_client=router_client,
            service_command=service_command,
            backup_path=backup_path,
            validation_result=validation_result,
        )
    except Exception as error:
        if temporary_pending:
            _remove_temporary_file(router_client, temporary_path)
        if backup_created and not isinstance(error, RouterUpdateError):
            raise RouterUpdateError(
                "Обновление прервалось после создания резервной копии. "
                f"Проверьте конфиг вручную; резервная копия: {backup_path}. "
                f"Причина: {error}"
            ) from error
        raise


def build_backup_path(config_path: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{config_path}.bak-{timestamp}"


def build_temporary_path(config_path: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    return f"{config_path}.tmp-{timestamp}"


def _dump_config_text(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _validate_service_name(service_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name):
        raise RouterUpdateError("Недопустимое имя службы OpenWrt.")


def _remove_temporary_file(router_client: RouterClient, path: str) -> None:
    _remove_file(router_client, path)


def _remove_file(router_client: RouterClient, path: str) -> CommandResult:
    command = f"rm -f {shlex.quote(path)}"
    try:
        return router_client.run(command)
    except Exception as error:
        return CommandResult(command=command, exit_code=-1, stderr=str(error))


def _run_or_failure(router_client: RouterClient, command: str) -> CommandResult:
    """Convert lost SSH responses into a failure that can trigger rollback."""
    try:
        return router_client.run(command)
    except Exception as error:
        return CommandResult(command=command, exit_code=-1, stderr=str(error))


def _rollback_after_service_failure(
    *,
    plan: RouterUpdatePlan,
    router_client: RouterClient,
    service_command: str,
    backup_path: str,
    validation_result: CommandResult,
) -> RouterUpdateResult:
    try:
        router_client.copy_file(backup_path, plan.config_path)
        rollback_restart_result = router_client.run(f"{service_command} restart")
        rollback_status_result = router_client.run(f"{service_command} status")
        rollback_success = (
            rollback_restart_result.exit_code == 0
            and rollback_status_result.exit_code == 0
        )
    except Exception as error:
        raise RouterUpdateError(
            "sing-box не запустился с новым конфигом, и автоматический откат "
            f"завершился ошибкой: {error}. Резервная копия: {backup_path}"
        ) from error

    if rollback_success:
        backup_cleanup_result = _remove_file(router_client, backup_path)
        backup_deleted = backup_cleanup_result.exit_code == 0
        if backup_deleted:
            message = (
                "sing-box не запустился с новым конфигом. Предыдущий конфиг "
                "автоматически восстановлен; временная резервная копия удалена."
            )
        else:
            message = (
                "sing-box не запустился с новым конфигом. Предыдущий конфиг "
                "автоматически восстановлен, но удалить временную резервную "
                "копию не удалось."
            )
    else:
        backup_cleanup_result = None
        backup_deleted = False
        message = (
            "Новый конфиг отменён, но sing-box не запустился после отката. "
            f"Проверьте роутер вручную. Резервная копия: {backup_path}"
        )

    return RouterUpdateResult(
        success=False,
        backup_path=backup_path,
        validation_result=validation_result,
        rollback_success=rollback_success,
        backup_deleted=backup_deleted,
        message=message,
    )


def _command_failure_message(label: str, result: CommandResult) -> str:
    details = result.stderr.strip() or result.stdout.strip() or "без описания"
    return f"{label} (код {result.exit_code}): {details}"
