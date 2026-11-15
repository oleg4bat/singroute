"""Coordinate engine selection, independent updates, and guarded service migration."""

from __future__ import annotations

import secrets
import shlex
import sys
from dataclasses import dataclass, replace

from .router_client import RouterClient
from .router_connection import (
    RouterEngine,
    RouterInfo,
    _running_config_path,
    refresh_service_states,
)
from .router_service import service_running_state, wait_for_service_state
from .router_update import (
    RouterConfigChangedError,
    RouterUpdateError,
    RouterUpdatePlan,
    RouterUpdateResult,
    _acquire_update_lock,
    _build_update_paths,
    _release_update_lock,
    _restart_and_verify_service,
    _run_or_failure,
    apply_router_update,
    prepare_router_update,
)
from .xray_service_config import XrayServiceConfig


@dataclass(frozen=True)
class EngineUpdatePlan:
    primary: RouterUpdatePlan
    engine: RouterEngine
    router_info: RouterInfo
    secondary: RouterUpdatePlan | None = None
    secondary_engine: RouterEngine | None = None
    secondary_error: str = ""
    source_path: str | None = None
    source_text: str | None = None

    @property
    def switches_engine(self) -> bool:
        return not self.engine.running

    @property
    def has_changes(self) -> bool:
        return (
            self.primary.has_changes
            or self.switches_engine
            or (self.engine.name == "xray" and self.engine.xray_enabled == "0")
            or bool(self.secondary and self.secondary.has_changes)
        )

    def applied_router_info(self) -> RouterInfo:
        """Update the connection snapshot from a confirmed successful apply."""
        switched = self.switches_engine or (
            self.engine.name == "xray" and self.engine.xray_enabled == "0"
        )
        return replace(
            self.router_info,
            engines=tuple(
                replace(
                    engine,
                    running=engine.name == self.engine.name
                    if switched
                    else engine.running,
                    boot_enabled=engine.name == self.engine.name
                    if switched and (not engine.issue or engine.running)
                    else engine.boot_enabled,
                    xray_enabled="1"
                    if engine.name == self.engine.name == "xray"
                    else engine.xray_enabled,
                )
                for engine in self.router_info.engines
            ),
        )


def prepare_engine_update(
    imported_text: str,
    client: RouterClient,
    info: RouterInfo,
    target: str,
    update_both: bool = False,
    transfer: bool = False,
) -> EngineUpdatePlan:
    engines = info.available_engines
    engine = next((e for e in engines if e.name == target), None)
    if engine is None or engine.issue:
        raise RouterUpdateError(
            engine.issue if engine else "Выбранный движок не установлен."
        )
    if any(e.service_state_issue for e in engines):
        raise RouterUpdateError(
            "Не удалось подтвердить состояние служб. Обновите подключение перед применением; конфиги доступны для просмотра."
        )
    if sum(e.running for e in engines) > 1:
        raise RouterUpdateError(
            "Обе службы работают. Остановите одну перед обновлением."
        )
    other = next((e for e in engines if e.name != target), None)
    source_path = source_text = None
    if transfer:
        if other is None or other.issue:
            raise RouterUpdateError("Другой движок не готов к переносу сервера.")
        source_path = other.config_path
        source_text = client.read_text(source_path)
        imported_text = source_text
    primary = prepare_router_update(
        imported_text, client, engine.config_path, engine.name
    )
    if not engine.running:
        active = next((e for e in engines if e.running), None)
        if active:
            active_text = client.read_text(active.config_path)
            if source_path is None:
                source_path, source_text = active.config_path, active_text
    secondary = None
    error = ""
    if update_both:
        if other is None:
            raise RouterUpdateError("Для обновления обоих конфигов нужны оба движка.")
        try:
            if other.issue:
                raise RouterUpdateError(other.issue)
            secondary = prepare_router_update(
                imported_text, client, other.config_path, other.name
            )
        except (ValueError, RuntimeError) as exc:
            # Converter errors are safe; never include remote validation output.
            error = f"Конфиг {other.name} не подготовлен: {type(exc).__name__}. Обновится только {target}."
    return EngineUpdatePlan(
        primary,
        engine,
        info,
        secondary,
        other if update_both else None,
        error,
        source_path,
        source_text,
    )


def _restart_engine_and_verify(client: RouterClient, engine: RouterEngine) -> bool:
    try:
        return (
            _restart_and_verify_service(
                client, shlex.quote("/etc/init.d/" + engine.name)
            )
            and _running_config_path(client, engine.name) == engine.config_path
        )
    except (ValueError, RuntimeError):
        return False


def _verify_inactive_engine(client: RouterClient, engine: RouterEngine) -> bool:
    try:
        result = client.run(f"{shlex.quote('/etc/init.d/' + engine.name)} status")
        return service_running_state(result) is False
    except (ValueError, RuntimeError):
        return False


class _ServiceSwitch:
    def __init__(
        self,
        client: RouterClient,
        target: RouterEngine,
        engines: tuple[RouterEngine, ...],
        activation_settings: XrayServiceConfig | None = None,
    ) -> None:
        self.client, self.target, self.engines = client, target, engines
        self.activation_settings = activation_settings
        self.failure_reason = ""

    def _run(self, engine: RouterEngine, action: str) -> bool:
        return (
            _run_or_failure(
                self.client,
                f"{shlex.quote('/etc/init.d/' + engine.service_name)} {action}",
            ).exit_code
            == 0
        )

    def _stopped(self, engine: RouterEngine) -> bool:
        return wait_for_service_state(
            lambda command: _run_or_failure(self.client, command),
            shlex.quote("/etc/init.d/" + engine.service_name),
            running=False,
        )

    def activate(self) -> bool:
        try:
            for engine in self.engines:
                if engine.name != self.target.name and engine.running:
                    self.failure_reason = f"Не удалось подтвердить остановку {engine.name}; {self.target.name} не запускался."
                    if not self._run(engine, "stop") or not self._stopped(engine):
                        return False
            self.failure_reason = (
                f"Не удалось настроить службу {self.target.name}; запуск отменён."
            )
            if (
                self.activation_settings is not None
                and not self.activation_settings.install()
            ):
                return False
            if (
                self.target.name == "xray"
                and self.target.xray_enabled == "0"
                and self.activation_settings is None
            ):
                return False
            self.failure_reason = (
                f"Не удалось подтвердить запуск службы {self.target.name}."
            )
            if not _restart_engine_and_verify(self.client, self.target):
                return False
            # Persist the selected service, preserving the old flags on rollback.
            for engine in self.engines:
                if engine.issue and not engine.running:
                    continue
                self.failure_reason = (
                    f"Не удалось сохранить автозапуск службы {engine.name}."
                )
                if not self._run(
                    engine, "enable" if engine.name == self.target.name else "disable"
                ):
                    return False
            self.failure_reason = ""
            return True
        except Exception:
            return False

    def restore(self) -> bool:
        ok = self._run(self.target, "stop") and self._stopped(self.target)
        if self.activation_settings is not None:
            ok = self.activation_settings.restore() and ok
        for engine in self.engines:
            if engine.issue and not engine.running:
                continue
            ok = (
                self._run(engine, "enable" if engine.boot_enabled else "disable") and ok
            )
            if engine.running:
                # Avoid starting the old engine if stopping the new one is uncertain.
                if not ok:
                    return False
                ok = _restart_engine_and_verify(self.client, engine) and ok
        return ok


def apply_engine_update(
    plan: EngineUpdatePlan,
    client: RouterClient,
) -> RouterUpdateResult:
    operation_id = secrets.token_hex(16)
    lock = _build_update_paths("/tmp/singroute-engines", None, operation_id)
    _acquire_update_lock(client, lock, operation_id)
    try:
        fresh = refresh_service_states(
            client,
            plan.router_info,
            include_boot=plan.switches_engine or plan.engine.xray_enabled == "0",
        )
        if any(engine.service_state_issue for engine in fresh.engines) or any(
            before.running != after.running
            for before, after in zip(
                plan.router_info.engines, fresh.engines, strict=True
            )
        ):
            raise RouterConfigChangedError(
                "Состояние служб изменилось. Переподключитесь к роутеру."
            )
        if plan.source_path and client.read_text(plan.source_path) != plan.source_text:
            raise RouterConfigChangedError(
                "Исходный конфиг изменился после подготовки переноса."
            )
        activation_settings = (
            XrayServiceConfig(client, plan.engine, operation_id)
            if plan.engine.name == "xray" and plan.engine.xray_enabled == "0"
            else None
        )
        switch = (
            _ServiceSwitch(
                client, plan.engine, fresh.available_engines, activation_settings
            )
            if plan.switches_engine or activation_settings is not None
            else None
        )
        primary = None
        try:
            primary = apply_router_update(
                plan.primary,
                client,
                service_name=plan.engine.service_name,
                verify_service=switch.activate
                if switch
                else lambda: _restart_engine_and_verify(client, plan.engine),
                restore_service=switch.restore
                if switch
                else lambda: _restart_engine_and_verify(client, plan.engine),
                xray_asset_directory=plan.engine.xray_asset_directory
                if plan.engine.name == "xray"
                else None,
            )
        finally:
            if activation_settings is not None:
                cleanup_message = activation_settings.close(
                    primary is not None
                    and (primary.success or primary.rollback_success is True)
                )
                if primary is not None and cleanup_message:
                    primary = replace(
                        primary,
                        details=primary.details + "\n" + cleanup_message,
                    )
        if not primary.success and switch and switch.failure_reason:
            primary = replace(
                primary,
                message=switch.failure_reason
                + (
                    " Предыдущий конфиг и состояние служб восстановлены."
                    if primary.rollback_success is True
                    else " Восстановление служб не подтверждено; проверьте роутер вручную."
                    + (
                        f" Резервная копия: {primary.backup_path}."
                        if primary.backup_path
                        else ""
                    )
                ),
                details=primary.details + "\n" + switch.failure_reason,
            )
        if not primary.success:
            if not plan.secondary or (
                primary.validation_result.exit_code == 0
                and primary.rollback_success is not True
            ):
                return primary
            # Independent updates: a safely rejected primary does not prevent
            # updating the other engine. An ambiguous rollback stops all work.
            try:
                other = plan.secondary_engine

                def verify_other() -> bool:
                    return (
                        _restart_engine_and_verify(client, other)
                        if other.running
                        else _verify_inactive_engine(client, other)
                    )

                secondary = apply_router_update(
                    plan.secondary,
                    client,
                    service_name=other.service_name,
                    verify_service=verify_other,
                    restore_service=verify_other,
                    xray_asset_directory=other.xray_asset_directory
                    if other.name == "xray"
                    else None,
                )
                message = f"{plan.engine.name} не обновлён; конфиг {other.name} " + (
                    "обновлён." if secondary.success else "тоже не обновлён."
                )
                return replace(
                    primary,
                    message=message,
                    details=primary.message + "\n" + secondary.message,
                    partial=secondary.success,
                    service_state_known=primary.service_state_known
                    and secondary.service_state_known,
                )
            except Exception:
                return replace(
                    primary,
                    service_state_known=False,
                    details=primary.message
                    + "\nСостояние второго конфига не подтверждено. Проверьте роутер и резервные копии вручную.",
                )
        error = plan.secondary_error
        service_state_known = primary.service_state_known
        if plan.secondary and plan.secondary.has_changes:
            try:
                # The inactive engine must remain stopped, even on another port.
                passive = apply_router_update(
                    plan.secondary,
                    client,
                    service_name=plan.secondary_engine.service_name,
                    verify_service=lambda: _verify_inactive_engine(
                        client, plan.secondary_engine
                    ),
                    restore_service=lambda: _verify_inactive_engine(
                        client, plan.secondary_engine
                    ),
                    xray_asset_directory=plan.secondary_engine.xray_asset_directory
                    if plan.secondary_engine.name == "xray"
                    else None,
                )
                if not passive.success:
                    service_state_known = passive.service_state_known
                    error = (
                        f"Конфиг {plan.secondary.engine} не обновлён. {passive.message}"
                    )
                elif passive.backup_path and not passive.backup_deleted:
                    error = f"Конфиг {plan.secondary.engine} обновлён, но осталась резервная копия: {passive.backup_path}."
            except Exception:
                service_state_known = False
                # The guarded workflow retains recovery files on ambiguous failure.
                error = f"Обновление {plan.secondary.engine} прервалось. Его состояние не подтверждено; проверьте конфиг и резервные копии на роутере."
        message = f"Готово — работает {plan.engine.name}."
        if error:
            message = f"Обновлён {plan.engine.name}; второй конфиг требует внимания."
        elif plan.secondary:
            message = f"Оба конфига обновлены; работает {plan.engine.name}."
        return replace(
            primary,
            message=message,
            details=primary.details + ("\n" + error if error else ""),
            partial=bool(error),
            service_state_known=service_state_known,
        )
    finally:
        operation_error = sys.exception()
        try:
            _release_update_lock(client, lock, operation_id)
        except RouterUpdateError as error:
            if operation_error is None:
                raise
            operation_error.add_note(str(error))
