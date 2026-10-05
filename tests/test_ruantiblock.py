from __future__ import annotations

from collections import deque

import pytest

from singroute.application.router_client import CommandResult
from singroute.application.ruantiblock import (
    SERVICE_CHECK,
    START_COMMAND,
    STATUS_COMMAND,
    RuantiblockError,
    RuantiblockState,
    inspect_ruantiblock,
    start_ruantiblock,
)


class ServiceClient:
    def __init__(self, *states: int, present: bool = True) -> None:
        self.states = deque(states)
        self.present = present
        self.start_result = CommandResult(START_COMMAND, 0)
        self.calls: list[str] = []
        self.last_state = 2

    def run(self, command: str) -> CommandResult:
        self.calls.append(command)
        if command == SERVICE_CHECK:
            return CommandResult(command, 0 if self.present else 1)
        if command == START_COMMAND:
            return self.start_result
        assert command == STATUS_COMMAND
        if self.states:
            self.last_state = self.states.popleft()
        return CommandResult(command, self.last_state, f"{self.last_state}\n")


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (0, RuantiblockState.ENABLED),
        (1, RuantiblockState.UNKNOWN),
        (2, RuantiblockState.DISABLED),
        (3, RuantiblockState.STARTING),
        (4, RuantiblockState.UPDATING),
        (-1, RuantiblockState.UNKNOWN),
        (127, RuantiblockState.UNKNOWN),
    ],
)
def test_raw_status_distinguishes_disabled_from_busy_or_error(code, state):
    client = ServiceClient(code)
    assert inspect_ruantiblock(client) == state
    assert START_COMMAND not in client.calls


def test_absent_service_does_not_query_status():
    client = ServiceClient(present=False)
    assert inspect_ruantiblock(client) == RuantiblockState.ABSENT
    assert client.calls == [SERVICE_CHECK]


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (2, "", ""),
        (2, "2\n", "config failed: secret"),
        (1, "Usage: raw-status is unsupported", ""),
        (0, "2", ""),
        (-1, "2", ""),
    ],
)
def test_ambiguous_status_never_means_disabled(code, stdout, stderr):
    class AmbiguousClient(ServiceClient):
        def run(self, command):
            if command == STATUS_COMMAND:
                return CommandResult(command, code, stdout, stderr)
            return super().run(command)

    assert inspect_ruantiblock(AmbiguousClient()) == RuantiblockState.UNKNOWN


def test_start_is_followed_by_verification_and_does_not_enable_autostart():
    client = ServiceClient(2, 3, 0)
    assert start_ruantiblock(client, poll_interval=0) == RuantiblockState.ENABLED
    assert client.calls == [
        SERVICE_CHECK,
        STATUS_COMMAND,
        START_COMMAND,
        SERVICE_CHECK,
        STATUS_COMMAND,
        SERVICE_CHECK,
        STATUS_COMMAND,
    ]


@pytest.mark.parametrize("state", [0, 3, 4])
def test_service_that_became_active_while_dialog_was_open_is_not_restarted(state):
    client = ServiceClient(state, 0)
    start_ruantiblock(client, poll_interval=0)
    assert START_COMMAND not in client.calls


@pytest.mark.parametrize("code", [1, -1])
def test_failed_or_ambiguous_start_is_not_success_or_retried(code):
    client = ServiceClient(2)
    client.start_result = CommandResult(START_COMMAND, code, "secret", "secret")
    with pytest.raises(RuantiblockError) as caught:
        start_ruantiblock(client)
    assert "secret" not in str(caught.value)
    assert client.calls.count(START_COMMAND) == 1


@pytest.mark.parametrize("state", [2, 3])
def test_successful_command_without_active_state_does_not_report_success(state):
    client = ServiceClient(2, state)
    with pytest.raises(RuantiblockError, match="не подтверждён"):
        start_ruantiblock(client, verification_timeout=0)


@pytest.mark.parametrize("state", [1, 127])
def test_changed_or_unknown_service_is_not_started(state):
    client = ServiceClient(state)
    with pytest.raises(RuantiblockError):
        start_ruantiblock(client)
    assert START_COMMAND not in client.calls


def test_service_removed_while_dialog_was_open_is_not_started():
    client = ServiceClient(present=False)
    with pytest.raises(RuantiblockError):
        start_ruantiblock(client)
    assert START_COMMAND not in client.calls


def test_unknown_status_after_start_does_not_report_success():
    client = ServiceClient(2, 1)
    with pytest.raises(RuantiblockError, match="проверить запуск"):
        start_ruantiblock(client)


def test_active_blacklist_update_confirms_activation():
    client = ServiceClient(2, 4)
    assert start_ruantiblock(client) == RuantiblockState.UPDATING
