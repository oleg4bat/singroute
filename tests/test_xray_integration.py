"""Exercise generated Xray exits and updates with real local engine processes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_auto_outbounds import UUID, xray_server
from test_auto_outbounds_integration import probe_target, proxy_request
from test_vless_ws_integration import _unused_ports
from test_xray_outbounds import native_group, tiered_group
from xray_test_router import LocalRouter, loopback_ssh_client, wait_port

from singroute.application.engine_update import (
    EngineUpdatePlan,
    apply_engine_update,
    prepare_engine_update,
)
from singroute.application.operation import (
    prepare_config_update,
    validate_imported_config,
)
from singroute.application.router_client import CommandResult
from singroute.application.router_connection import inspect_router
from singroute.application.router_update import (
    apply_router_update,
    prepare_router_update,
)
from singroute.application.xray_service_config import XrayServiceConfig
from singroute.core.converters.xray import to_xray


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


def setup_local(router):
    backend, proxy, other = _unused_ports(3)
    router.put(
        "/etc/sing-box/config.json",
        json.dumps(
            {
                "inbounds": [
                    {"type": "mixed", "listen": "127.0.0.1", "listen_port": other}
                ],
                "outbounds": [{"type": "direct", "tag": "proxy"}],
            }
        ),
    )
    router.put(
        "/etc/xray/config.json",
        json.dumps(
            {
                "inbounds": [
                    {"protocol": "http", "listen": "127.0.0.1", "port": proxy}
                ],
                "outbounds": [
                    {"protocol": "freedom", "tag": "direct"},
                    xray_server("proxy"),
                ],
            }
        ),
    )
    # Start with a selectable first server that is irrelevant to direct local traffic.
    config = json.loads(router.read_text("/etc/xray/config.json"))
    config["outbounds"] = [
        xray_server("proxy"),
        {"protocol": "freedom", "tag": "direct"},
    ]
    router.put("/etc/xray/config.json", json.dumps(config))
    backend_config = {
        "log": {"loglevel": "debug"},
        "inbounds": [
            {
                "protocol": "vless",
                "listen": "127.0.0.1",
                "port": backend,
                "settings": {"clients": [{"id": UUID}], "decryption": "none"},
                "streamSettings": {"network": "tcp", "security": "none"},
            }
        ],
        "outbounds": [
            {
                "protocol": "freedom",
                "settings": {
                    "finalRules": [{"action": "allow", "ip": ["127.0.0.1/32"]}]
                },
            }
        ],
    }
    path = router.root / "backend.json"
    path.write_text(json.dumps(backend_config), encoding="utf-8")
    log = (router.root / "backend.log").open("w", encoding="utf-8")
    router.logs.append(log)
    process = subprocess.Popen(
        [router.binaries["xray"], "run", "-config", str(path)],
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    router.processes["backend"] = process
    wait_port(backend)
    source = {"outbounds": [xray_server("imported", port=backend)]}
    source["outbounds"][0]["settings"]["vnext"][0]["address"] = "127.0.0.1"
    return source, proxy, other


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
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    target = next(e for e in info.engines if e.name == "xray")
    primary = prepare_router_update(
        json.dumps(source), local_router, target.config_path, "xray"
    )
    # Exercise migration with real local HTTP inputs supported on Windows.
    plan = EngineUpdatePlan(primary, target, info)
    local_router.fail_restart = "xray"
    result = apply_engine_update(
        plan, local_router, "/etc/sing-box/config.json", "sing-box"
    )
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
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    target = next(e for e in info.engines if e.name == "xray")
    primary = prepare_router_update(
        json.dumps(source), local_router, target.config_path, "xray"
    )
    assert primary.preview["old_outbound"] == {"outbounds": []}
    result = apply_engine_update(
        EngineUpdatePlan(primary, target, info),
        local_router,
        "/etc/sing-box/config.json",
        "sing-box",
    )
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
        info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
        plan = prepare_engine_update(json.dumps(source), client, info, "xray")
        result = apply_engine_update(
            plan, client, "/etc/sing-box/config.json", "sing-box"
        )
    assert result.success
    assert "sleep 1; /etc/init.d/sing-box status" in local_router.calls
    assert "sleep 1; /etc/init.d/xray status" in local_router.calls
    assert not local_router.overlapping_engines
    assert local_router.running("xray") and not local_router.running("sing-box")
    with probe_target() as target:
        assert proxy_request(proxy, target)


@pytest.mark.parametrize("failure", [None, "restart", "stage", "pending", "validation"])
def test_transfer_uses_only_main_xray_file_and_restores_service_settings_on_failure(
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
    local_router.set_uci_option("/etc/config/xray", "xray.config.confdir", "/etc/xray")
    local_router.flag = "0"
    assert local_router.start("sing-box")
    original = local_router.read_text("/etc/xray/config.json")
    original_settings = local_router.read_text("/etc/config/xray")
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update("", local_router, info, "xray", transfer=True)
    assert plan.primary.preview["old_outbound"] == {"outbounds": []}
    assert "только /etc/xray/config.json" in plan.primary.warnings[0]
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
    result = apply_engine_update(
        plan, local_router, "/etc/sing-box/config.json", "sing-box"
    )
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
        assert "xray.config.confdir" not in settings
        missing_directory = local_router.run("uci -q get xray.config.confdir")
        assert missing_directory.exit_code == 1 and missing_directory.stdout == ""
        assert settings["xray.enabled.enabled"] == "1"
        assert settings["xray.config.dialer"] == "preserved-dialer"
        assert not local_router.running("sing-box") and local_router.running("xray")
        assert not local_router.lose_uci_install_response
        with probe_target() as target:
            assert proxy_request(proxy, target)
    assert not list(local_router.path("/etc/config").glob(".xray.singroute-*"))


@pytest.mark.parametrize(
    "code,value,accepted",
    [
        (0, "", True),
        (1, "", True),
        (-1, "", False),
        (127, "", False),
        (1, "/etc/xray", False),
    ],
)
def test_optional_confdir_accepts_absence_but_not_failed_or_nonempty_reads(
    local_router, code, value, accepted
):
    setup_local(local_router)
    local_router.set_uci_option("/etc/config/xray", "xray.config.confdir", "/etc/xray")
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    original = local_router.read_text("/etc/config/xray")
    real_run = local_router.run

    def optional_get(command):
        if command == "uci -q get xray.config.confdir":
            return CommandResult(command, code, value)
        return real_run(command)

    local_router.run = optional_get
    settings = XrayServiceConfig(local_router, info.engines[1], "a" * 32)
    try:
        assert settings.install() is accepted
        assert settings.restore()
        assert local_router.read_text("/etc/config/xray") == original
    finally:
        settings.close(safe=True)


def test_secondary_check_failure_keeps_working_primary_and_inactive_original(
    local_router,
):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update(
        json.dumps(source), local_router, info, "xray", update_both=True
    )
    before = local_router.read_text("/etc/sing-box/config.json")
    local_router.fail_check = "sing-box"
    result = apply_engine_update(
        plan, local_router, "/etc/sing-box/config.json", "sing-box"
    )
    assert result.success and result.partial
    assert "второй конфиг" in result.message
    assert local_router.read_text("/etc/sing-box/config.json") == before
    assert not local_router.running("sing-box")
    with probe_target() as target:
        assert proxy_request(proxy, target)


def test_both_configs_update_without_starting_inactive_engine(local_router):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update(
        json.dumps(source), local_router, info, "xray", update_both=True
    )
    result = apply_engine_update(
        plan, local_router, "/etc/sing-box/config.json", "sing-box"
    )
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
    from test_xray_outbounds import xray_router

    source = (Path(__file__).parent / "fixtures/source_xray_leastload.json").read_text(
        encoding="utf-8"
    )
    updated = prepare_config_update(source, json.dumps(xray_router()), "xray")
    config = tmp_path / "config.new"
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
    info = inspect_router(local_router, "/etc/sing-box/config.json", "sing-box")
    plan = prepare_engine_update(
        json.dumps(source), local_router, info, "xray", update_both=True
    )
    before = local_router.read_text("/etc/xray/config.json")
    local_router.fail_check = "xray"
    result = apply_engine_update(
        plan, local_router, "/etc/sing-box/config.json", "sing-box"
    )
    assert not result.success and result.partial
    assert local_router.read_text("/etc/xray/config.json") == before
    assert (
        json.loads(local_router.read_text("/etc/sing-box/config.json"))["outbounds"][0][
            "server"
        ]
        == "127.0.0.1"
    )
    assert not local_router.running("sing-box")


def test_xray_update_over_real_ssh_uses_pinned_key_and_does_not_log_secrets(
    local_router,
):
    source, proxy, _ = setup_local(local_router)
    assert local_router.start("xray")
    with loopback_ssh_client(local_router) as (client, progress):
        info = inspect_router(client, "/etc/sing-box/config.json", "sing-box")
        plan = prepare_engine_update(json.dumps(source), client, info, "xray")
        result = apply_engine_update(
            plan, client, "/etc/sing-box/config.json", "sing-box"
        )
        assert result.success
        assert UUID not in "\n".join(progress)
        assert "test-password" not in "\n".join(progress)
        with probe_target() as target:
            assert proxy_request(proxy, target)
