from __future__ import annotations

from dataclasses import replace

import pytest
from router_shell import ShellRouter

from singroute.application.router_client import run_commands
from singroute.application.router_connection import (
    RouterCompatibilityError,
    inspect_router,
)


@pytest.mark.parametrize("openwrt", [True, False])
def test_discovery_checks_the_host_and_reads_both_services_in_one_request(
    tmp_path, monkeypatch, openwrt
):
    router = ShellRouter(tmp_path, {"sing-box", "xray"})
    if not openwrt:
        run = router.run

        def other_os(command):
            # Change the device's release response, keeping the real shell framing.
            result = run(command)
            return replace(result, stdout=result.stdout.replace("OpenWrt", "OtherOS"))

        monkeypatch.setattr(router, "run", other_os)
        with pytest.raises(RouterCompatibilityError, match="OpenWrt"):
            inspect_router(router)
        return
    progress = []
    info = inspect_router(router, progress.append)
    assert progress
    assert info.openwrt_release == "OpenWrt local-test"
    assert {e.name for e in info.engines} == {"sing-box", "xray"}
    assert all(e.running and not e.issue for e in info.engines)
    assert {e.config_path for e in info.engines} == {
        "/etc/sing-box/config.json",
        "/etc/xray/config.json",
    }
    assert len(router.calls) == 1


def test_batch_round_trip_preserves_results_and_rejects_an_incomplete_reply(tmp_path):
    router = ShellRouter(tmp_path, set())
    commands = {
        "text": "printf 'first\\nlast'",
        "failed": "printf 'diagnostic' >&2; false",
        "empty": "true",
    }
    results = run_commands(router, commands, "Batch integration")
    assert len(router.calls) == 1
    assert results["text"].stdout == "first\nlast"
    assert results["failed"].exit_code == 1
    assert results["failed"].stdout == "diagnostic"
    assert results["empty"].exit_code == 0 and results["empty"].stdout == ""

    class TruncatedReply:
        def run(self, command):
            result = router.run(command)
            return replace(result, stdout=result.stdout[:-5])

    with pytest.raises(RuntimeError, match="состояние роутера"):
        run_commands(TruncatedReply(), commands, "Interrupted batch")
