"""Application use case for updating a router sing-box config."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import posixpath
import re
import secrets
import shlex
from typing import Any

from singroute.application.operation import prepare_config_update, summarize_router_outbound
from singroute.application.router_client import CommandResult, RouterClient


DEFAULT_CONFIG_PATH = "/etc/sing-box/config.json"
DEFAULT_SERVICE_NAME = "sing-box"
DEFAULT_SING_BOX_COMMAND = "sing-box"
INSTALL_IO_ERROR_EXIT = 74
CONFIG_CHANGED_EXIT = 75
UPDATE_LOCKED_EXIT = 76
_OPERATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


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


@dataclass(frozen=True)
class _UpdatePaths:
    operation_directory: str
    temporary_path: str
    backup_path: str
    restore_path: str
    lock_path: str


def prepare_router_update(
    imported_config_content: str,
    router_client: RouterClient,
    config_path: str = DEFAULT_CONFIG_PATH,
) -> RouterUpdatePlan:
    """Read the router and prepare a preview without changing remote state."""
    _validate_config_path(config_path)
    original_config_text = router_client.read_text(config_path)
    update = prepare_config_update(imported_config_content, original_config_text)
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
    _validate_config_path(config_path)
    return summarize_router_outbound(router_client.read_text(config_path))


def apply_router_update(
    plan: RouterUpdatePlan,
    router_client: RouterClient,
    service_name: str = DEFAULT_SERVICE_NAME,
    sing_box_command: str = DEFAULT_SING_BOX_COMMAND,
    now: datetime | None = None,
    operation_id: str | None = None,
) -> RouterUpdateResult:
    """Validate, compare-and-swap, restart, and roll back safely."""
    _validate_config_path(plan.config_path)
    _validate_service_name(service_name)
    operation_id = operation_id or secrets.token_hex(16)
    _validate_operation_id(operation_id)
    paths = _build_update_paths(plan.config_path, now, operation_id)

    current_config_text = router_client.read_text(plan.config_path)
    if current_config_text != plan.original_config_text:
        raise RouterConfigChangedError(
            "Конфиг роутера изменился после подготовки превью. "
            "Обновите превью и повторите операцию."
        )

    operation_directory_created = False
    backup_available = False
    temporary_pending = False
    validation_result = CommandResult("sing-box check", -1)
    try:
        _create_operation_directory(router_client, paths.operation_directory)
        operation_directory_created = True
        router_client.write_text(paths.temporary_path, plan.updated_config_text)
        temporary_pending = True

        validation_result = router_client.run(
            f"{shlex.quote(sing_box_command)} check -c "
            f"{shlex.quote(paths.temporary_path)}"
        )
        if validation_result.exit_code != 0:
            _cleanup_operation_directory(router_client, paths, remove_backup=True)
            operation_directory_created = False
            temporary_pending = False
            return RouterUpdateResult(
                success=False,
                backup_path=None,
                validation_result=validation_result,
                message=(
                    "Новый конфиг не прошёл проверку sing-box; "
                    "текущий конфиг не изменён."
                ),
            )

        install_result = _run_or_failure(
            router_client,
            _build_guarded_install_command(plan, paths),
        )
        if install_result.exit_code == CONFIG_CHANGED_EXIT:
            _cleanup_operation_directory(router_client, paths, remove_backup=True)
            operation_directory_created = False
            temporary_pending = False
            raise RouterConfigChangedError(
                "Конфиг роутера изменился во время проверки. "
                "Изменения не установлены; обновите превью."
            )
        if install_result.exit_code == UPDATE_LOCKED_EXIT:
            _cleanup_operation_directory(router_client, paths, remove_backup=True)
            operation_directory_created = False
            temporary_pending = False
            raise RouterUpdateError(
                "Другой экземпляр SingRoute уже обновляет этот конфиг. "
                "Дождитесь завершения операции и повторите попытку."
            )

        if install_result.exit_code == 0:
            backup_available = True
            temporary_pending = False
        else:
            state, backup_available = _reconcile_install_state(
                plan,
                router_client,
                paths,
            )
            if state == "original":
                _cleanup_operation_directory(router_client, paths, remove_backup=True)
                operation_directory_created = False
                temporary_pending = False
                raise RouterUpdateError(
                    _command_failure_message("Новый конфиг не был установлен", install_result)
                )
            if state != "updated" or not backup_available:
                temporary_pending = False
                raise RouterUpdateError(
                    "Не удалось однозначно определить состояние конфига после "
                    "потери SSH-ответа. Автоматические действия остановлены; "
                    f"проверьте роутер вручную. Резервная копия: {paths.backup_path}"
                )
            temporary_pending = False

        service_command = shlex.quote(f"/etc/init.d/{service_name}")
        restart_result = _run_or_failure(router_client, f"{service_command} restart")
        status_result = _run_or_failure(router_client, f"{service_command} status")
        if restart_result.exit_code == 0 and status_result.exit_code == 0:
            backup_deleted = _cleanup_operation_directory(
                router_client,
                paths,
                remove_backup=True,
            )
            operation_directory_created = not backup_deleted
            message = (
                "Конфиг обновлён, sing-box успешно перезапущен; "
                "временная резервная копия удалена."
                if backup_deleted
                else "Конфиг обновлён и sing-box успешно перезапущен, но удалить "
                "временную резервную копию не удалось."
            )
            return RouterUpdateResult(
                success=True,
                backup_path=paths.backup_path,
                validation_result=validation_result,
                backup_deleted=backup_deleted,
                message=message,
            )

        return _rollback_after_service_failure(
            plan=plan,
            router_client=router_client,
            service_command=service_command,
            paths=paths,
            validation_result=validation_result,
        )
    except Exception as error:
        if operation_directory_created and not backup_available:
            _cleanup_operation_directory(router_client, paths, remove_backup=True)
        elif temporary_pending:
            _remove_file(router_client, paths.temporary_path)
        if backup_available and not isinstance(error, RouterUpdateError):
            raise RouterUpdateError(
                "Обновление прервалось после создания резервной копии. "
                f"Проверьте конфиг вручную; резервная копия: {paths.backup_path}. "
                f"Причина: {error}"
            ) from error
        raise


def build_backup_path(
    config_path: str,
    now: datetime | None = None,
    operation_id: str | None = None,
) -> str:
    operation_id = operation_id or secrets.token_hex(16)
    return _build_update_paths(config_path, now, operation_id).backup_path


def build_temporary_path(
    config_path: str,
    now: datetime | None = None,
    operation_id: str | None = None,
) -> str:
    operation_id = operation_id or secrets.token_hex(16)
    return _build_update_paths(config_path, now, operation_id).temporary_path


def _build_update_paths(
    config_path: str,
    now: datetime | None,
    operation_id: str,
) -> _UpdatePaths:
    _validate_config_path(config_path)
    _validate_operation_id(operation_id)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    parent = posixpath.dirname(config_path)
    name = posixpath.basename(config_path)
    operation_directory = posixpath.join(
        parent,
        f".{name}.singroute-{timestamp}-{operation_id}",
    )
    return _UpdatePaths(
        operation_directory=operation_directory,
        temporary_path=posixpath.join(operation_directory, "config.new"),
        backup_path=posixpath.join(operation_directory, "config.backup"),
        restore_path=posixpath.join(operation_directory, "config.restore"),
        lock_path=posixpath.join(parent, f".{name}.singroute-update.lock"),
    )


def _dump_config_text(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _validate_config_path(config_path: str) -> None:
    if (
        not config_path.startswith("/")
        or config_path == "/"
        or posixpath.normpath(config_path) != config_path
        or any(ord(character) < 32 for character in config_path)
    ):
        raise RouterUpdateError("Недопустимый абсолютный путь конфига OpenWrt.")


def _validate_service_name(service_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name):
        raise RouterUpdateError("Недопустимое имя службы OpenWrt.")


def _validate_operation_id(operation_id: str) -> None:
    if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise RouterUpdateError("Некорректный идентификатор операции обновления.")


def _create_operation_directory(router_client: RouterClient, path: str) -> None:
    command = f"umask 077; mkdir {shlex.quote(path)}"
    result = router_client.run(command)
    if result.exit_code != 0:
        raise RouterUpdateError(
            _command_failure_message("Не удалось создать приватный каталог обновления", result)
        )


def _build_guarded_install_command(plan: RouterUpdatePlan, paths: _UpdatePaths) -> str:
    expected_digest = hashlib.sha256(
        plan.original_config_text.encode("utf-8")
    ).hexdigest()
    lock_cleanup = f"rmdir {shlex.quote(paths.lock_path)} >/dev/null 2>&1"
    return (
        "umask 077; "
        f"if ! mkdir {shlex.quote(paths.lock_path)}; then "
        f"exit {UPDATE_LOCKED_EXIT}; fi; "
        f"trap {shlex.quote(lock_cleanup)} 0 1 2 15; "
        f"cp -p {shlex.quote(plan.config_path)} {shlex.quote(paths.backup_path)} "
        f"|| exit {INSTALL_IO_ERROR_EXIT}; "
        f"actual=$(sha256sum {shlex.quote(paths.backup_path)}) "
        f"|| exit {INSTALL_IO_ERROR_EXIT}; "
        'actual=${actual%% *}; '
        f"if [ \"$actual\" != {shlex.quote(expected_digest)} ]; then "
        f"rm -f {shlex.quote(paths.backup_path)}; "
        f"exit {CONFIG_CHANGED_EXIT}; fi; "
        f"mv -f {shlex.quote(paths.temporary_path)} "
        f"{shlex.quote(plan.config_path)} || exit {INSTALL_IO_ERROR_EXIT}"
    )


def _reconcile_install_state(
    plan: RouterUpdatePlan,
    router_client: RouterClient,
    paths: _UpdatePaths,
) -> tuple[str, bool]:
    try:
        current = router_client.read_text(plan.config_path)
    except Exception:
        return "unknown", _file_exists(router_client, paths.backup_path)
    backup_available = _file_exists(router_client, paths.backup_path)
    if current == plan.updated_config_text:
        return "updated", backup_available
    if current == plan.original_config_text:
        return "original", backup_available
    return "unknown", backup_available


def _file_exists(router_client: RouterClient, path: str) -> bool:
    try:
        return router_client.run(f"test -f {shlex.quote(path)}").exit_code == 0
    except Exception:
        return False


def _cleanup_operation_directory(
    router_client: RouterClient,
    paths: _UpdatePaths,
    *,
    remove_backup: bool,
) -> bool:
    cleanup_ok = True
    for path in (paths.temporary_path, paths.restore_path):
        cleanup_ok = _remove_file(router_client, path).exit_code == 0 and cleanup_ok
    if remove_backup and cleanup_ok:
        cleanup_ok = (
            _remove_file(router_client, paths.backup_path).exit_code == 0
            and cleanup_ok
        )
    if remove_backup and cleanup_ok:
        result = _run_or_failure(
            router_client,
            f"rmdir {shlex.quote(paths.operation_directory)}",
        )
        cleanup_ok = result.exit_code == 0 and cleanup_ok
    return cleanup_ok


def _remove_file(router_client: RouterClient, path: str) -> CommandResult:
    command = f"rm -f {shlex.quote(path)}"
    try:
        return router_client.run(command)
    except Exception as error:
        return CommandResult(command=command, exit_code=-1, stderr=str(error))


def _run_or_failure(router_client: RouterClient, command: str) -> CommandResult:
    """Convert lost SSH responses into a result that can be reconciled."""
    try:
        return router_client.run(command)
    except Exception as error:
        return CommandResult(command=command, exit_code=-1, stderr=str(error))


def _rollback_after_service_failure(
    *,
    plan: RouterUpdatePlan,
    router_client: RouterClient,
    service_command: str,
    paths: _UpdatePaths,
    validation_result: CommandResult,
) -> RouterUpdateResult:
    restore_command = (
        f"cp -p {shlex.quote(paths.backup_path)} "
        f"{shlex.quote(paths.restore_path)} && "
        f"mv -f {shlex.quote(paths.restore_path)} "
        f"{shlex.quote(plan.config_path)}"
    )
    restore_result = _run_or_failure(router_client, restore_command)
    restored = restore_result.exit_code == 0
    if not restored:
        try:
            restored = router_client.read_text(plan.config_path) == plan.original_config_text
        except Exception:
            restored = False
    if not restored:
        raise RouterUpdateError(
            "sing-box не запустился с новым конфигом, и автоматический откат "
            "не удалось подтвердить. "
            f"Резервная копия: {paths.backup_path}. "
            f"Причина: {_command_failure_message('ошибка восстановления', restore_result)}"
        )

    rollback_restart_result = _run_or_failure(
        router_client,
        f"{service_command} restart",
    )
    rollback_status_result = _run_or_failure(
        router_client,
        f"{service_command} status",
    )
    rollback_success = (
        rollback_restart_result.exit_code == 0
        and rollback_status_result.exit_code == 0
    )

    if rollback_success:
        backup_deleted = _cleanup_operation_directory(
            router_client,
            paths,
            remove_backup=True,
        )
        message = (
            "sing-box не запустился с новым конфигом. Предыдущий конфиг "
            "автоматически восстановлен; временная резервная копия удалена."
            if backup_deleted
            else "sing-box не запустился с новым конфигом. Предыдущий конфиг "
            "автоматически восстановлен, но удалить временную резервную "
            "копию не удалось."
        )
    else:
        backup_deleted = False
        message = (
            "Новый конфиг отменён, но sing-box не запустился после отката. "
            f"Проверьте роутер вручную. Резервная копия: {paths.backup_path}"
        )

    return RouterUpdateResult(
        success=False,
        backup_path=paths.backup_path,
        validation_result=validation_result,
        rollback_success=rollback_success,
        backup_deleted=backup_deleted,
        message=message,
    )


def _command_failure_message(label: str, result: CommandResult) -> str:
    details = result.stderr.strip() or result.stdout.strip() or "без описания"
    return f"{label} (код {result.exit_code}): {details}"
