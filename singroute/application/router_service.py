"""Interpret OpenWrt service status without confusing stopped and unknown."""

from __future__ import annotations

from collections.abc import Callable

from .router_client import CommandResult

SERVICE_TRANSITION_POLLS = 10


def wait_for_service_state(
    run: Callable[[str], CommandResult], service_command: str, *, running: bool
) -> bool:
    """Wait for procd's asynchronous transition, without retrying unknown state."""
    for attempt in range(SERVICE_TRANSITION_POLLS + 1):
        command = ("sleep 1; " if attempt else "") + service_command + " status"
        state = service_running_state(run(command))
        if state is running:
            return True
        if state is None:
            return False
    return False


def service_running_state(result: CommandResult) -> bool | None:
    """procd: 3 = inactive, 5 = not running, 4 = unknown instance.

    A registered service with no process instances can return zero, so its
    explicit diagnostic must take precedence over that exit status.
    """
    if result.exit_code in {1, 3, 5}:
        return False
    if result.exit_code != 0:
        return None
    text = result.stdout.strip().casefold()
    return text not in {
        "active with no instances",
        "not running",
        "inactive",
        "stopped",
    }
