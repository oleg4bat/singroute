"""Exercise generated Xray exits and updates with real local engine processes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from config_samples import UUID, native_group, tiered_group, xray_server
from local_proxy import _unused_ports, probe_target, proxy_request
from ssh_test_server import loopback_ssh_client
from xray_test_router import LocalRouter, setup_local, wait_port

from singroute.application.engine_update import (
    EngineUpdatePlan,
    apply_engine_update,
    prepare_engine_update,
)
from singroute.application.operation import (
    prepare_config_update,
    validate_imported_config,
)
from singroute.application.router_connection import inspect_router
from singroute.application.router_update import (
    RouterConfigChangedError,
    _build_update_paths,
    apply_router_update,
    prepare_router_update,
)
from singroute.core.converters.xray import to_xray


def test_real_xray_failure_is_logged_without_echoed_credentials(tmp_path):
    from singroute.gui.main_window import _result_details

    secret = "invalid-secret-key"
    server = xray_server("proxy")
    server["streamSettings"].update(
        security="reality",
        realitySettings={
            "publicKey": secret,
            "serverName": "example.test",
            "shortId": "0123456789abcdef",
        },
    )
    raw_output = []

    class RealValidationRouter(LocalRouter):
        def run(self, command, input_text=None):
            result = super().run(command, input_text)
            if command.startswith("xray run -test "):
                raw_output.append(result.stdout + result.stderr)
            return result

    router = RealValidationRouter(tmp_path, required_engines=("xray",))
    config_path = "/etc/xray/config.json"
    try:
        router.put(config_path, json.dumps({"outbounds": []}))
        plan = prepare_router_update(
            json.dumps({"outbounds": [server]}), router, config_path, "xray"
        )
        result = apply_router_update(plan, router, service_name="xray")
        assert not result.success
        assert secret in raw_output[0]
        details = _result_details(result)
        assert 'invalid "password"' in details
        assert secret not in details
        assert "***" in details
        assert json.loads(router.read_text(config_path)) == {"outbounds": []}
    finally:
        router.close()


@pytest.mark.parametrize("engine", ["sing-box", "xray"])
def test_flat_happ_metrics_import_passes_real_engine_check(tmp_path, engine):
    binary = os.environ.get(
        "XRAY_BINARY" if engine == "xray" else "SING_BOX_BINARY"
    ) or shutil.which(engine)
    if not binary:
        pytest.skip(f"Set a binary for real {engine} validation")
    source = (
        Path(__file__).parent / "fixtures/source_happ_vless_metrics.json"
    ).read_text(encoding="utf-8")
    router = (
        {"outbounds": []}
        if engine == "xray"
        else {"outbounds": [{"type": "direct", "tag": "proxy"}]}
    )
    update = prepare_config_update(source, json.dumps(router), engine)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(update.updated_config), encoding="utf-8")
    args = ["run", "-test", "-config"] if engine == "xray" else ["check", "-c"]
    checked = subprocess.run(
        [binary, *args, str(config)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize(
    "fixture",
    [
        "source_happ_vless_reality_1.json",
        "source_happ_vless_reality_grpc.json",
        "source_happ_vless_ws_tls.json",
        "source_happ_trojan_tls.json",
        "source_happ_hysteria2.json",
    ],
)
def test_converted_connection_passes_real_xray_test(tmp_path, fixture):
    binary = os.environ.get("XRAY_BINARY") or shutil.which("xray")
    if not binary:
        pytest.skip("Set XRAY_BINARY for real Xray validation")
    source = (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
    server = to_xray(validate_imported_config(source).servers[0])
    # Legacy fixtures intentionally contain invalid secret placeholders.
    if fixture == "source_happ_vless_reality_1.json":
        server["settings"]["vnext"][0]["users"][0]["id"] = UUID
        server["streamSettings"]["realitySettings"].update(
            publicKey="CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            shortId="0123456789abcdef",
        )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"outbounds": [server]}), encoding="utf-8")
    result = subprocess.run(
        [binary, "run", "-test", "-config", str(config)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def local_router(tmp_path):
    router = LocalRouter(tmp_path)
    yield router
    router.close()


@pytest.mark.parametrize("when", ["before_validation", "during_validation"])
def test_concurrent_config_edit_is_preserved_over_real_ssh(
    local_router, monkeypatch, when
):
    source, _, _ = setup_local(local_router)
    assert local_router.start("xray")
    process = local_router.processes["xray"]
    config_path = "/etc/xray/config.json"
    changed_config = json.loads(local_router.read_text(config_path))
    changed_config["log"] = {"loglevel": "error"}
    changed = json.dumps(changed_config)
    paths_before = set(local_router.root.rglob("*"))
    real_run = local_router.run
    validated = []

    def run(command, input_text=None):
        result = real_run(command, input_text)
        if "xray run -test " in command:
            validated.append(result.exit_code)
            if when == "during_validation":
                local_router.put(config_path, changed)
        return result

    monkeypatch.setattr(local_router, "run", run)
    with loopback_ssh_client(local_router) as (client, _):
        plan = prepare_engine_update(
            json.dumps(source), client, inspect_router(client), "xray"
        )
        if when == "before_validation":
            local_router.put(config_path, changed)
        with pytest.raises(RouterConfigChangedError, match="изменился"):
            apply_engine_update(plan, client)
        assert validated == [0]
        assert local_router.read_text(config_path) == changed
        assert local_router.processes["xray"] is process
        assert process.poll() is None
        assert set(local_router.root.rglob("*")) == paths_before
        assert client.run("cat /etc/openwrt_release").exit_code == 0


def test_real_xray_update_replaces_files_and_carries_vless_traffic(local_router):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    plan = prepare_router_update(
        json.dumps(source), local_router, "/etc/xray/config.json", "xray"
    )
    local_router.lose_install_response = True
    result = apply_router_update(plan, local_router, service_name="xray")
    assert result.success and result.backup_deleted
    wait_port(proxy)
    with probe_target() as target:
        assert proxy_request(proxy, target)
    assert json.loads(local_router.read_text(plan.config_path))["outbounds"][-1] == {
        "protocol": "freedom",
        "tag": "direct",
    }


def test_real_switch_failure_restores_files_flags_and_old_process(local_router):
    source, _, other = setup_local(local_router)
    assert local_router.start("sing-box")
    info = inspect_router(local_router)
    target = next(e for e in info.engines if e.name == "xray")
    primary = prepare_router_update(
        json.dumps(source), local_router, target.config_path, "xray"
    )
    # Exercise migration with real local HTTP inputs supported on Windows.
    plan = EngineUpdatePlan(primary, target, info)
    local_router.fail_restart = "xray"
    result = apply_engine_update(plan, local_router)
    assert not result.success and result.rollback_success
    assert local_router.read_text(primary.config_path) == primary.original_config_text
    assert local_router.running("sing-box") and not local_router.running("xray")
    assert local_router.boot == {"sing-box": True, "xray": False}
    with probe_target() as target_port:
        assert proxy_request(other, target_port)


def test_real_switch_keeps_one_process_running_and_changes_boot_flags(local_router):
    source, proxy, _ = setup_local(local_router)
    config = json.loads(local_router.read_text("/etc/xray/config.json"))
    config["outbounds"] = []
    local_router.put("/etc/xray/config.json", json.dumps(config))
    local_router.boot["xray"] = True
    local_router.flag = "0"
    assert local_router.start("sing-box")
    info = inspect_router(local_router)
    target = next(e for e in info.engines if e.name == "xray")
    primary = prepare_router_update(
        json.dumps(source), local_router, target.config_path, "xray"
    )
    assert primary.preview["old_outbound"] == {"outbounds": []}
    result = apply_engine_update(EngineUpdatePlan(primary, target, info), local_router)
    assert result.success
    assert local_router.flag == "1"
    assert not local_router.running("sing-box") and local_router.running("xray")
    assert local_router.boot == {"sing-box": False, "xray": True}
    with probe_target() as target_port:
        assert proxy_request(proxy, target_port)


def test_switch_waits_for_actual_delayed_process_stop_and_start_over_ssh(local_router):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("sing-box")
    local_router.transition_delays = {
        ("sing-box", "stop"): 0.7,
        ("xray", "restart"): 0.7,
    }
    with loopback_ssh_client(local_router) as (client, _progress):
        info = inspect_router(client)
        plan = prepare_engine_update(json.dumps(source), client, info, "xray")
        result = apply_engine_update(plan, client)
    assert result.success
    assert not local_router.overlapping_engines
    assert local_router.running("xray") and not local_router.running("sing-box")
    with probe_target() as target:
        assert proxy_request(proxy, target)


@pytest.mark.parametrize("failure", [None, "restart", "stage", "pending", "validation"])
def test_transfer_keeps_xray_file_selection_and_restores_enabled_flag_on_failure(
    local_router, failure
):
    source, proxy, other = setup_local(local_router)
    singbox = json.loads(local_router.read_text("/etc/sing-box/config.json"))
    singbox["outbounds"] = [
        {
            "type": "vless",
            "tag": "proxy",
            "server": "127.0.0.1",
            "server_port": source["outbounds"][0]["settings"]["vnext"][0]["port"],
            "uuid": UUID,
        }
    ]
    local_router.put("/etc/sing-box/config.json", json.dumps(singbox))
    xray = json.loads(local_router.read_text("/etc/xray/config.json"))
    xray["outbounds"] = []
    local_router.put("/etc/xray/config.json", json.dumps(xray))
    examples = {
        "/etc/xray/vpoint_socks_vmess.json": '{"outbounds": [{"protocol": "freedom", "tag": "sample-one"}]}',
        "/etc/xray/vpoint_vmess_freedom.json": '{"outbounds": [{"protocol": "freedom", "tag": "sample-two"}]}',
    }
    for path, text in examples.items():
        local_router.put(path, text)
    local_router.flag = "0"
    assert local_router.start("sing-box")
    original = local_router.read_text("/etc/xray/config.json")
    original_settings = local_router.read_text("/etc/config/xray")
    info = inspect_router(local_router)
    plan = prepare_engine_update("", local_router, info, "xray", transfer=True)
    assert plan.primary.preview["old_outbound"] == {"outbounds": []}
    assert not plan.primary.warnings
    if failure == "restart":
        local_router.fail_restart = "xray"
    elif failure == "stage":
        local_router.fail_uci_stage = True
    elif failure == "pending":
        local_router.pending_uci = "xray.config.dialer='user pending change'"
    elif failure == "validation":
        local_router.fail_check = "xray"
    else:
        local_router.lose_uci_install_response = True
    result = apply_engine_update(plan, local_router)
    for path, text in examples.items():
        assert local_router.read_text(path) == text
    if failure:
        assert not result.success
        if failure != "validation":
            assert result.rollback_success
        assert local_router.read_text("/etc/xray/config.json") == original
        assert local_router.read_text("/etc/config/xray") == original_settings
        assert local_router.running("sing-box") and not local_router.running("xray")
        assert local_router.pending_uci == (
            "xray.config.dialer='user pending change'" if failure == "pending" else ""
        )
        with probe_target() as target:
            assert proxy_request(other, target)
    else:
        assert result.success
        settings = local_router.uci_values(local_router.read_text("/etc/config/xray"))
        assert settings["xray.config.conffiles"] == "/etc/xray/config.json"
        assert settings["xray.config.confdir"] == ""
        missing_directory = local_router.run("uci -q get xray.config.confdir")
        assert missing_directory.exit_code == 0 and missing_directory.stdout == ""
        assert settings["xray.enabled.enabled"] == "1"
        assert settings["xray.config.dialer"] == "preserved-dialer"
        assert not local_router.running("sing-box") and local_router.running("xray")
        assert not local_router.lose_uci_install_response
        with probe_target() as target:
            assert proxy_request(proxy, target)
    assert not list(local_router.path("/etc/config").glob(".xray.singroute-*"))


def test_secondary_check_failure_keeps_working_primary_and_inactive_original(
    local_router,
):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    info = inspect_router(local_router)
    plan = prepare_engine_update(
        json.dumps(source), local_router, info, "xray", update_both=True
    )
    before = local_router.read_text("/etc/sing-box/config.json")
    local_router.fail_check = "sing-box"
    result = apply_engine_update(plan, local_router)
    assert result.success and result.partial
    assert "второй конфиг" in result.message
    assert local_router.read_text("/etc/sing-box/config.json") == before
    assert not local_router.running("sing-box")
    with probe_target() as target:
        assert proxy_request(proxy, target)


def test_both_configs_update_without_starting_inactive_engine(local_router):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    with loopback_ssh_client(local_router) as (client, _):
        info = inspect_router(client)
        plan = prepare_engine_update(
            json.dumps(source), client, info, "xray", update_both=True
        )
        before_apply = len(local_router.calls)
        result = apply_engine_update(plan, client)
        assert len(local_router.calls) - before_apply <= 24
    assert result.success and not result.partial
    assert "Оба конфига" in result.message
    assert not local_router.running("sing-box")
    assert (
        json.loads(local_router.read_text("/etc/sing-box/config.json"))["outbounds"][0][
            "server"
        ]
        == "127.0.0.1"
    )
    with probe_target() as target:
        assert proxy_request(proxy, target)


def test_native_leastload_group_passes_real_xray_and_carries_traffic(local_router):
    source, proxy, _ = setup_local(local_router)
    server = source["outbounds"][0]
    group = native_group(2)
    for index in range(2):
        group["outbounds"][index] = {**server, "tag": f"proxy-{index}"}
    group["outbounds"][0] = json.loads(json.dumps(group["outbounds"][0]))
    group["outbounds"][0]["settings"]["vnext"][0]["port"] = _unused_ports(1)[0]
    group["routing"]["balancers"][0].pop("fallbackTag")
    with probe_target() as target:
        group["burstObservatory"]["pingConfig"].update(
            destination=f"http://127.0.0.1:{target}/probe", interval="1s", timeout="1s"
        )
        config = json.loads(local_router.read_text("/etc/xray/config.json"))
        result = prepare_config_update(json.dumps(group), json.dumps(config), "xray")
        local_router.put("/etc/xray/config.json", json.dumps(result.updated_config))
        assert local_router.start("xray")
        wait_port(proxy)
        deadline = time.monotonic() + 20
        while not proxy_request(proxy, target):
            assert time.monotonic() < deadline, (
                "leastLoad did not select the working server"
            )
            time.sleep(0.1)


def test_anonymized_user_group_passes_real_xray_validation(tmp_path):
    binary = os.environ.get("XRAY_BINARY") or shutil.which("xray")
    if not binary:
        pytest.skip("Set XRAY_BINARY for real Xray validation")
    from config_samples import xray_router

    source = (Path(__file__).parent / "fixtures/source_xray_leastload.json").read_text(
        encoding="utf-8"
    )
    updated = prepare_config_update(source, json.dumps(xray_router()), "xray")
    # Exercise the production staging name: recent Xray versions reject .new
    # even when the caller explicitly supplies -format json.
    staged = _build_update_paths("/etc/xray/config.json", None, "a" * 32)
    config = tmp_path / Path(staged.temporary_path).name
    config.write_text(json.dumps(updated.updated_config), encoding="utf-8")
    result = subprocess.run(
        [binary, "run", "-test", "-format", "json", "-config", str(config)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("working", ["second-group", "last-server"])
def test_native_tiered_fallback_routes_through_loopback_and_final_server(
    local_router, working
):
    source, proxy, _ = setup_local(local_router)
    group = tiered_group()
    dead_port = _unused_ports(1)[0]
    for index, entry in enumerate(group["outbounds"]):
        if entry["protocol"] != "vless":
            continue
        tag = entry["tag"]
        server = json.loads(json.dumps(source["outbounds"][0]))
        server["tag"] = tag
        live = (
            tag in {f"cand{i:02}" for i in range(4, 10)}
            if working == "second-group"
            else tag == "warmup_outbound"
        )
        if not live:
            server["settings"]["vnext"][0]["port"] = dead_port
        group["outbounds"][index] = server
    with probe_target() as target:
        group["burstObservatory"]["pingConfig"].update(
            destination=f"http://127.0.0.1:{target}/probe",
            interval="1s",
            timeout="500ms",
            sampling=1,
        )
        result = prepare_config_update(
            json.dumps(group), local_router.read_text("/etc/xray/config.json"), "xray"
        )
        local_router.put("/etc/xray/config.json", json.dumps(result.updated_config))
        assert local_router.start("xray")
        wait_port(proxy)
        deadline = time.monotonic() + 15
        while not proxy_request(proxy, target):
            assert time.monotonic() < deadline, "Native tiered fallback did not route"
            time.sleep(0.1)


def test_primary_validation_failure_still_updates_other_inactive_config(local_router):
    source, _, _ = setup_local(local_router)
    assert local_router.start("xray")
    info = inspect_router(local_router)
    plan = prepare_engine_update(
        json.dumps(source), local_router, info, "xray", update_both=True
    )
    before = local_router.read_text("/etc/xray/config.json")
    local_router.fail_check = "xray"
    result = apply_engine_update(plan, local_router)
    assert not result.success and result.partial
    assert local_router.read_text("/etc/xray/config.json") == before
    assert (
        json.loads(local_router.read_text("/etc/sing-box/config.json"))["outbounds"][0][
            "server"
        ]
        == "127.0.0.1"
    )
    assert not local_router.running("sing-box")


@pytest.mark.parametrize("failure", [None, "restart", "stage", "pending", "validation"])
def test_enabling_xray_preserves_service_file_selection_and_rolls_back(
    tmp_path, failure
):
    router = LocalRouter(tmp_path, required_engines=("xray",))
    try:
        source, proxy, _ = setup_local(router)
        router.flag = "0"
        original_settings = router.read_text("/etc/config/xray")
        original_json = router.read_text("/etc/xray/config.json")
        with loopback_ssh_client(router) as (client, _):
            info = inspect_router(client)
            plan = prepare_engine_update(json.dumps(source), client, info, "xray")
            router.fail_restart = "xray" if failure == "restart" else None
            router.fail_uci_stage = failure == "stage"
            router.fail_check = "xray" if failure == "validation" else None
            if failure == "pending":
                router.pending_uci = "xray.config.dialer='pending change'"
            result = apply_engine_update(plan, client)
            assert result.success is (failure is None)
            settings = router.uci_values(router.read_text("/etc/config/xray"))
            assert settings["xray.config.conffiles"] == "/etc/xray/config.json"
            assert settings["xray.config.confdir"] == ""
            if failure:
                assert router.read_text("/etc/config/xray") == original_settings
                assert router.read_text("/etc/xray/config.json") == original_json
                assert not router.running("xray")
                assert router.pending_uci == (
                    "xray.config.dialer='pending change'"
                    if failure == "pending"
                    else ""
                )
            else:
                assert settings["xray.enabled.enabled"] == "1"
                with probe_target() as target:
                    assert proxy_request(proxy, target)
    finally:
        router.close()
