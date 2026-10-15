"""Xray policy extraction, isolation, and conversion regression tests."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_auto_outbounds import UUID, xray_server

from singroute.application.operation import (
    prepare_config_update,
    validate_imported_config,
)
from singroute.core.converters.xray import to_xray
from singroute.core.errors import ConfigPatchError


def native_group(count=12):
    servers = [xray_server(f"proxy-{index}") for index in range(count)]
    return {
        "log": {"access": "/client/log.txt"},
        "dns": {"servers": ["https://source.test/dns-query"]},
        "inbounds": [{"protocol": "socks", "port": 10808}],
        "outbounds": [
            *servers,
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
        "burstObservatory": {
            "subjectSelector": ["proxy"],
            "pingConfig": {
                "connectivity": "",
                "destination": "http://www.gstatic.com/generate_204",
                "interval": "2m",
                "sampling": 3,
                "timeout": "3s",
            },
        },
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "domainMatcher": "hybrid",
            "balancers": [
                {
                    "tag": "Auto_Balancer",
                    "selector": ["proxy"],
                    "fallbackTag": "proxy-0",
                    "strategy": {
                        "type": "leastLoad",
                        "settings": {
                            "baselines": ["200ms", "500ms"],
                            "costs": [{"match": "bypass", "regexp": True, "value": 3}],
                            "expected": 7,
                            "maxRTT": "1500ms",
                            "tolerance": 0,
                        },
                    },
                }
            ],
            "rules": [
                {"type": "field", "domain": ["domain:ru"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "Auto_Balancer"},
            ],
        },
    }


def xray_router():
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [
            {
                "tag": "tproxy-in",
                "protocol": "dokodemo-door",
                "listen": "0.0.0.0",
                "port": 1100,
                "settings": {"network": "tcp,udp", "followRedirect": True},
                "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
            }
        ],
        "outbounds": [],
        "routing": {"domainStrategy": "AsIs"},
    }


def update(source, router=None):
    return prepare_config_update(
        json.dumps(source),
        json.dumps(router if router is not None else xray_router()),
        "xray",
    )


def test_imported_group_keeps_native_policy_but_not_client_configuration():
    source, router = native_group(), xray_router()
    original = deepcopy((source, router))
    result = update(source, router)
    config = result.updated_config
    assert len(config["outbounds"]) == 12
    assert config["inbounds"] == router["inbounds"]
    assert config["log"] == router["log"]
    assert "dns" not in config
    assert config["routing"]["domainStrategy"] == "AsIs"
    assert "domainMatcher" not in config["routing"]
    assert (
        config["routing"]["balancers"][0]["strategy"]
        == source["routing"]["balancers"][0]["strategy"]
    )
    assert config["burstObservatory"] == source["burstObservatory"]
    assert config["routing"]["rules"] == [
        {"type": "field", "network": "tcp,udp", "balancerTag": "singroute-auto"}
    ]
    assert UUID not in json.dumps(result.preview)
    assert "routing" in result.preview["new_outbound"]
    assert (source, router) == original
    assert not update(source, config).has_changes


def test_anonymized_user_export_retains_all_twelve_reality_servers_and_native_policy():
    source = json.loads(
        (Path(__file__).parent / "fixtures/source_xray_leastload.json").read_text(
            encoding="utf-8"
        )
    )
    result = update(source)
    assert len(result.updated_config["outbounds"]) == 12
    assert result.updated_config["burstObservatory"] == source["burstObservatory"]
    assert (
        result.updated_config["routing"]["balancers"][0]["strategy"]
        == source["routing"]["balancers"][0]["strategy"]
    )
    assert "dns" not in result.updated_config
    assert UUID not in json.dumps(result.preview)
    singbox = prepare_config_update(
        json.dumps(source),
        json.dumps({"outbounds": [{"type": "direct", "tag": "proxy"}]}),
    )
    assert len(singbox.updated_config["outbounds"]) == 13
    assert singbox.updated_config["outbounds"][0]["type"] == "urltest"
    assert not singbox.warnings


def tiered_group():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/source_xray_tiered_leastload.json"
        ).read_text(encoding="utf-8")
    )


def test_tiered_fallback_group_preserves_both_strategies_and_only_internal_routing():
    source = tiered_group()
    router = xray_router()
    result = update(source, router)
    config = result.updated_config
    assert len(config["outbounds"]) == 11
    assert config["inbounds"] == router["inbounds"]
    assert config["log"] == router["log"]
    assert "dns" not in config
    assert config["burstObservatory"] == source["burstObservatory"]
    assert [b["strategy"] for b in config["routing"]["balancers"]] == [
        b["strategy"] for b in source["routing"]["balancers"]
    ]
    assert config["routing"]["balancers"][0]["fallbackTag"] == "loop_to_t2"
    assert config["routing"]["balancers"][1]["fallbackTag"] == "warmup_outbound"
    assert config["routing"]["rules"] == [
        {"type": "field", "inboundTag": ["entry_t2"], "balancerTag": "bal_auto_2"},
        {"type": "field", "network": "tcp,udp", "balancerTag": "singroute-auto"},
    ]
    assert "dependent_balancers" in result.preview["new_outbound"]
    assert not update(source, config).has_changes
    replaced = update({"outbounds": [xray_server("new")]}, config).updated_config
    assert len(replaced["outbounds"]) == 1
    assert "balancers" not in replaced["routing"]
    assert "burstObservatory" not in replaced
    assert replaced["routing"]["rules"] == [
        {"type": "field", "network": "tcp,udp", "outboundTag": "cand01"}
    ]


@pytest.mark.parametrize("fault", ["cycle", "missing", "routing", "shared"])
def test_tiered_fallback_rejects_broken_or_shared_dependencies(fault):
    source, router = tiered_group(), xray_router()
    if fault == "cycle":
        source["routing"]["balancers"][1]["fallbackTag"] = "loop_to_t2"
    elif fault == "missing":
        source["routing"]["balancers"][1]["fallbackTag"] = "missing-server"
    elif fault == "routing":
        source["routing"]["rules"][-1]["domain"] = ["subset.example.test"]
    else:
        router = update(source, router).updated_config
        router["routing"]["rules"].insert(
            0,
            {
                "type": "field",
                "domain": ["private.example.test"],
                "balancerTag": "bal_auto_2",
            },
        )
        source = {"outbounds": [xray_server("new")]}
    with pytest.raises(ConfigPatchError):
        update(source, router)


def test_single_group_single_group_preserves_unrelated_outbounds_and_rules():
    router = xray_router()
    router["outbounds"] = [
        xray_server("router-proxy"),
        {"protocol": "freedom", "tag": "direct"},
    ]
    router["routing"]["rules"] = [
        {"type": "field", "domain": ["domain:local.test"], "outboundTag": "direct"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "router-proxy"},
    ]
    first = update(native_group(2), router).updated_config
    assert first["outbounds"][-1] == router["outbounds"][-1]
    assert first["routing"]["rules"][0] == router["routing"]["rules"][0]
    single = update({"outbounds": [xray_server("new")]}, first).updated_config
    assert len(single["outbounds"]) == 2
    assert "burstObservatory" not in single
    assert "balancers" not in single["routing"]
    assert (
        single["routing"]["rules"][-1]["outboundTag"] == single["outbounds"][0]["tag"]
    )
    again = update(native_group(3), single).updated_config
    last = update(native_group(1), again).updated_config
    assert len(last["outbounds"]) == 2
    assert last["outbounds"][-1] == router["outbounds"][-1]
    assert last["inbounds"] == router["inbounds"]


def test_native_xhttp_does_not_pass_through_singbox_conversion():
    server = xray_server("proxy", "xhttp")
    server["streamSettings"]["xhttpSettings"] = {
        "path": "/test",
        "mode": "auto",
        "extra": {"scMaxEachPostBytes": 200000},
    }
    result = update({"outbounds": [server]})
    assert (
        result.updated_config["outbounds"][0]["streamSettings"]
        == server["streamSettings"]
    )
    with pytest.raises(ConfigPatchError):
        validate_imported_config(json.dumps({"outbounds": [server]}), "sing-box")


def test_router_default_route_selects_the_referenced_server_and_preserves_tag():
    router = xray_router()
    router["outbounds"] = [xray_server("unrelated"), xray_server("active")]
    router["routing"]["rules"] = [
        {"type": "field", "network": "tcp,udp", "outboundTag": "active"}
    ]
    result = update({"outbounds": [xray_server("imported")]}, router).updated_config
    assert result["outbounds"][1]["tag"] == "active"
    assert result["outbounds"][0] == router["outbounds"][0]
    assert result["routing"] == router["routing"]


def test_scoped_proxy_rule_does_not_change_unrelated_default_exit():
    router = xray_router()
    router["outbounds"] = [
        {"protocol": "freedom", "tag": "direct"},
        xray_server("active"),
    ]
    router["routing"]["rules"] = [
        {
            "type": "field",
            "inboundTag": ["tproxy-in"],
            "network": "tcp,udp",
            "outboundTag": "active",
        }
    ]
    result = update(native_group(2), router).updated_config
    assert result["outbounds"][0] == router["outbounds"][0]
    assert len(result["routing"]["rules"]) == 1
    assert result["routing"]["rules"][0]["inboundTag"] == ["tproxy-in"]


@pytest.mark.parametrize("tag", ["proxy-local", "proxy-0"])
def test_group_prefix_collisions_abort_without_mutation(tag):
    router = xray_router()
    router["outbounds"] = [xray_server("old"), {"protocol": "freedom", "tag": tag}]
    before = deepcopy(router)
    with pytest.raises(ConfigPatchError, match="пересекаются"):
        update(native_group(1), router)
    assert router == before


def test_shared_existing_group_member_is_protected():
    router = update(native_group(2)).updated_config
    router["routing"]["rules"].insert(
        0, {"domain": ["domain:local.test"], "outboundTag": "proxy-0"}
    )
    with pytest.raises(ConfigPatchError, match="других настройках"):
        update({"outbounds": [xray_server("new")]}, router)


def test_monitor_from_another_group_is_never_overwritten():
    router = xray_router()
    router["outbounds"] = [xray_server("old"), xray_server("other")]
    router["burstObservatory"] = {
        "subjectSelector": ["other"],
        "pingConfig": {"interval": "5m"},
    }
    with pytest.raises(ConfigPatchError, match="другими выходами"):
        update(native_group(2), router)


@pytest.mark.parametrize("broken", ["monitor", "fallback", "dependency"])
def test_broken_group_is_rejected_as_a_whole(broken):
    source = native_group(2)
    if broken == "monitor":
        source.pop("burstObservatory")
    elif broken == "fallback":
        source["routing"]["balancers"][0]["fallbackTag"] = "missing"
    else:
        source["outbounds"][1]["streamSettings"]["sockopt"] = {"dialerProxy": "direct"}
    with pytest.raises(ConfigPatchError):
        update(source)


def test_native_strategy_changes_count_as_config_changes():
    source = native_group(2)
    router = update(source).updated_config
    source["routing"]["balancers"][0]["strategy"]["settings"]["expected"] = 1
    result = update(source, router)
    assert result.has_changes
    assert result.updated_config["outbounds"] == router["outbounds"]


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
def test_supported_singbox_connections_convert_back_to_xray(fixture):
    source = (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
    singbox = validate_imported_config(source).servers[0]
    converted = to_xray(singbox)
    assert converted["protocol"] in {"vless", "trojan", "hysteria"}
    assert (
        validate_imported_config(json.dumps({"outbounds": [converted]})).servers[0]
        == singbox
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("detour", "direct"),
        ("network", "tcp"),
        ("bind_interface", "eth0"),
        ("packet_encoding", "packetaddr"),
    ],
)
def test_unmapped_singbox_fields_are_never_silently_dropped(field, value):
    server = {
        "type": "vless",
        "server": "server.test",
        "server_port": 443,
        "uuid": UUID,
        field: value,
    }
    with pytest.raises(ConfigPatchError, match="безопасного соответствия"):
        to_xray(server)


def test_singbox_urltest_group_converts_with_an_explicit_policy_warning():
    source = {
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": ["node"]},
            {
                "type": "vless",
                "tag": "node",
                "server": "server.test",
                "server_port": 443,
                "uuid": UUID,
            },
        ]
    }
    result = update(source)
    assert result.warnings
    assert result.updated_config["routing"]["balancers"][0]["strategy"] == {
        "type": "leastPing"
    }
    assert result.updated_config["observatory"]["subjectSelector"] == [
        "singroute-node-"
    ]
