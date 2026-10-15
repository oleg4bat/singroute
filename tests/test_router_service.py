"""Regression tests for actual procd status meanings."""

import pytest

from singroute.application.router_client import CommandResult
from singroute.application.router_service import (
    SERVICE_TRANSITION_POLLS,
    service_running_state,
    wait_for_service_state,
)


@pytest.mark.parametrize(
    "code,text,expected",
    [
        (0, "running\n", True),
        (0, "running (1/2)\n", True),
        (0, "", True),
        (0, "active with no instances\n", False),
        (1, "", False),
        (3, "inactive\n", False),
        (5, "not running\n", False),
        (4, "unknown instance *\n", None),
        (127, "", None),
        (-1, "not running\n", None),
    ],
)
def test_openwrt_status_distinguishes_no_process_from_unknown(code, text, expected):
    assert (
        service_running_state(CommandResult("/etc/init.d/xray status", code, text))
        is expected
    )


@pytest.mark.parametrize("running", [True, False])
def test_wait_for_delayed_service_transition(running):
    commands = []
    states = iter([not running, not running, running])

    def run(command):
        commands.append(command)
        state = next(states)
        return CommandResult(command, 0 if state else 5)

    assert wait_for_service_state(run, "/etc/init.d/xray", running=running)
    assert commands == [
        "/etc/init.d/xray status",
        "sleep 1; /etc/init.d/xray status",
        "sleep 1; /etc/init.d/xray status",
    ]


@pytest.mark.parametrize("running", [True, False])
def test_service_transition_times_out_and_does_not_accept_unknown_state(running):
    commands = []

    def unchanged(command):
        commands.append(command)
        return CommandResult(command, 5 if running else 0)

    assert not wait_for_service_state(unchanged, "/etc/init.d/xray", running=running)
    assert len(commands) == SERVICE_TRANSITION_POLLS + 1
    commands.clear()

    def unknown(command):
        commands.append(command)
        return CommandResult(command, -1)

    assert not wait_for_service_state(unknown, "/etc/init.d/xray", running=running)
    assert commands == ["/etc/init.d/xray status"]
