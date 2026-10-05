"""Optional ruantiblock activation after a successful router update."""

from __future__ import annotations

import time
from enum import Enum

from singroute.application.router_client import RouterClient

SERVICE_CHECK = "test -x /etc/init.d/ruantiblock && test -x /usr/bin/ruantiblock"
STATUS_COMMAND = "/usr/bin/ruantiblock raw-status"
START_COMMAND = "/etc/init.d/ruantiblock start"


class RuantiblockState(Enum):
    ABSENT = "absent"
    ENABLED = "enabled"
    DISABLED = "disabled"
    STARTING = "starting"
    UPDATING = "updating"
    UNKNOWN = "unknown"


class RuantiblockError(RuntimeError):
    """Activation failed or could not be verified."""


def inspect_ruantiblock(client: RouterClient) -> RuantiblockState:
    """Use ruantiblock's own state, rather than procd or boot enablement.

    Upstream raw-status: 0 enabled, 1 error, 2 disabled, 3 starting, 4 updating.
    Require both the exit status and matching numeric output; an unsupported
    command, missing SSH exit status, or diagnostic must never mean disabled.
    """
    presence = client.run(SERVICE_CHECK)
    if presence.exit_code == 1 and not presence.stderr.strip():
        return RuantiblockState.ABSENT
    if presence.exit_code != 0 or presence.stderr.strip():
        return RuantiblockState.UNKNOWN
    result = client.run(STATUS_COMMAND)
    states = {
        0: RuantiblockState.ENABLED,
        2: RuantiblockState.DISABLED,
        3: RuantiblockState.STARTING,
        4: RuantiblockState.UPDATING,
    }
    if result.stdout.strip() != str(result.exit_code) or result.stderr.strip():
        return RuantiblockState.UNKNOWN
    return states.get(result.exit_code, RuantiblockState.UNKNOWN)


def start_ruantiblock(
    client: RouterClient,
    *,
    verification_timeout: float = 10.0,
    poll_interval: float = 0.5,
) -> RuantiblockState:
    """Recheck before starting, leave autostart alone, and verify activation."""
    state = inspect_ruantiblock(client)
    if state in {RuantiblockState.ENABLED, RuantiblockState.UPDATING}:
        return state
    if state == RuantiblockState.DISABLED:
        result = client.run(START_COMMAND)
        if result.exit_code != 0:
            # Do not log router output: it may include private configuration.
            raise RuantiblockError(
                "Команда запуска ruantiblock не завершилась успешно. "
                "Проверьте состояние службы на роутере."
            )
    elif state != RuantiblockState.STARTING:
        raise RuantiblockError("Не удалось определить состояние ruantiblock.")

    deadline = time.monotonic() + verification_timeout
    while True:
        state = inspect_ruantiblock(client)
        if state in {RuantiblockState.ENABLED, RuantiblockState.UPDATING}:
            return state
        if state not in {RuantiblockState.DISABLED, RuantiblockState.STARTING}:
            raise RuantiblockError("Не удалось проверить запуск ruantiblock.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuantiblockError("Запуск ruantiblock не подтверждён.")
        time.sleep(min(poll_interval, remaining))
