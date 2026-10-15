"""Regression cases for source groups and complete exit replacement."""

import json
from copy import deepcopy

import pytest

from singroute.application.operation import (
    prepare_config_update,
    summarize_router_outbound,
)
from singroute.core.auto_outbounds import managed_member_tags
from singroute.core.errors import ConfigPatchError, SourceGroupError

UUID = "00000000-0000-4000-8000-000000000001"


def xray_server(tag, transport="tcp", port=443):
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "server.example.test",
                    "port": port,
                    "users": [{"id": UUID, "encryption": "none"}],
                }
            ]
        },
        "streamSettings": {"network": transport, "security": "none"},
    }


def xray_group(*servers):
    return {
        "outbounds": list(servers),
        "routing": {
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "main"}],
            "balancers": [
                {
                    "tag": "main",
                    "selector": ["proxy"],
                    "strategy": {"type": "leastLoad", "settings": {"expected": 7}},
                }
            ],
        },
        "dns": {"servers": ["source-dns.example.test"]},
    }


def router_config():
    return {
        "dns": {"servers": ["router-dns.example.test"]},
        "inbounds": [{"type": "tun", "interface_name": "tun0", "auto_route": False}],
        "route": {"final": "router-proxy", "rules": [{"outbound": "direct"}]},
        "outbounds": [
            {
                "type": "vless",
                "tag": "router-proxy",
                "server": "old.example.test",
                "server_port": 443,
                "uuid": UUID,
            },
            {"type": "direct", "tag": "direct"},
        ],
        "experimental": {"cache_file": {"enabled": True}},
    }


def update(source, router=None):
    return prepare_config_update(
        json.dumps(source), json.dumps(router or router_config())
    )


def test_tiered_source_imports_only_primary_and_uses_prefix_matching():
    source = xray_group(
        xray_server("cand01"),
        xray_server("cand02"),
        xray_server("cand03"),
        xray_server("backup01"),
        {
            "protocol": "loopback",
            "tag": "fallback",
            "settings": {"inboundTag": "entry_t2"},
        },
    )
    routing = source["routing"]
    routing["balancers"][0].update(
        selector=["cand01", "cand02", "cand03"], fallbackTag="fallback"
    )
    routing["balancers"].insert(0, {"tag": "reserve", "selector": ["backup"]})
    routing["rules"].insert(0, {"inboundTag": ["entry_t2"], "balancerTag": "reserve"})
    result = update(source)
    outbounds = result.updated_config["outbounds"]
    assert len(outbounds) == 5
    assert outbounds[0] == {
        "type": "urltest",
        "tag": "router-proxy",
        "outbounds": [o["tag"] for o in outbounds[1:4]],
        "url": "https://www.gstatic.com/generate_204",
        "interval": "1m",
        "tolerance": 20,
        "interrupt_exist_connections": True,
    }
    assert result.warnings == ()
    assert "backup01" not in json.dumps(outbounds)
    assert "source-dns" not in json.dumps(result.updated_config)


def test_provider_with_one_prefix_and_twelve_servers_is_imported_entirely():
    # A suffix called "direct" is still a VLESS server, not a direct outbound.
    result = update(xray_group(*(xray_server(f"proxy-{i}-direct") for i in range(12))))
    assert len(result.updated_config["outbounds"][0]["outbounds"]) == 12
    assert len(result.updated_config["outbounds"]) == 14


def test_mixed_group_skips_unsupported_and_malformed_without_leaking_source_text():
    secret_tag = "proxy-password-DO-NOT-ECHO"
    result = update(
        xray_group(
            xray_server("proxy-good"),
            xray_server(secret_tag, "xhttp"),
            xray_server("proxy-bad", port=0),
        )
    )
    assert len(result.updated_config["outbounds"][0]["outbounds"]) == 1
    assert len(result.warnings) == 2
    assert "№2" in result.warnings[0] and "XHTTP" in result.warnings[0]
    assert "№3" in result.warnings[1]
    assert secret_tag not in str(result.warnings)


def test_no_compatible_primary_does_not_fall_back_to_other_tiers_or_mutate_input():
    source = xray_group(xray_server("proxy-bad", "xhttp"), xray_server("reserve"))
    router = router_config()
    original = deepcopy(router)
    with pytest.raises(SourceGroupError, match="нет совместимых"):
        update(source, router)
    assert router == original


def test_single_group_single_group_group_replaces_instead_of_accumulating():
    initial = router_config()
    first = update(
        xray_group(xray_server("proxy-1"), xray_server("proxy-2")), initial
    ).updated_config
    assert len(first["outbounds"]) == 4
    single = update({"outbounds": [xray_server("single")]}, first).updated_config
    assert len(single["outbounds"]) == 2
    assert single["outbounds"][0]["type"] == "vless"
    assert managed_member_tags(single) == set()
    second = update(
        xray_group(xray_server("proxy-3"), xray_server("proxy-4")), single
    ).updated_config
    replacement = xray_group(xray_server("proxy-5"))
    replacement["outbounds"][0]["settings"]["vnext"][0]["address"] = "replacement.test"
    last = update(replacement, second).updated_config
    assert len(last["outbounds"]) == 3
    assert last["outbounds"][1]["server"] == "replacement.test"
    for config in (first, single, second, last):
        assert {k: v for k, v in config.items() if k != "outbounds"} == {
            k: v for k, v in initial.items() if k != "outbounds"
        }
        assert config["outbounds"][-1] == initial["outbounds"][-1]
        assert config["outbounds"][0]["tag"] == "router-proxy"
    assert initial == router_config()


def test_reimport_is_unchanged_but_member_secret_change_is_detected():
    source = xray_group(xray_server("proxy-1"), xray_server("proxy-2"))
    first = update(source).updated_config
    assert not update(source, first).has_changes
    source["outbounds"][1]["settings"]["vnext"][0]["users"][0]["id"] = (
        "00000000-0000-4000-8000-000000000002"
    )
    changed = update(source, first)
    assert changed.has_changes
    assert changed.preview["old_outbound"] == changed.preview["new_outbound"]
    assert UUID not in json.dumps(changed.preview)
    summary = summarize_router_outbound(json.dumps(first))
    assert len(summary["servers"]) == 2
    assert summary["servers"][0]["uuid"] == "***"


def test_unowned_groups_and_unrelated_prefix_like_tags_are_preserved():
    router = router_config()
    router["outbounds"][0] = {
        "type": "urltest",
        "tag": "router-proxy",
        "outbounds": ["other-server"],
    }
    foreign = {"type": "vless", "tag": "other-server", "server": "other.test"}
    prefix_like = {"type": "direct", "tag": "singroute-auto-something-1"}
    router["outbounds"].extend([foreign, prefix_like])
    result = update(xray_group(xray_server("proxy-1")), router).updated_config
    assert result["outbounds"][-2:] == [foreign, prefix_like]


def test_name_collision_preserves_unrelated_node_and_external_references_stop_replacement():
    source = xray_group(xray_server("proxy-1"))
    first = update(source).updated_config
    member = first["outbounds"][1]
    router = router_config()
    router["outbounds"].append(member)
    replacement = update(source, router).updated_config
    assert replacement["outbounds"][0]["outbounds"] == ["proxy-1 (2)"]
    assert replacement["outbounds"][-1] == member
    first["route"]["rules"].append({"outbound": member["tag"]})
    with pytest.raises(ConfigPatchError, match="других настройках"):
        update({"outbounds": [xray_server("single")]}, first)


def test_ambiguous_or_missing_primary_is_not_guessed():
    source = xray_group(xray_server("proxy-1"))
    source["routing"]["balancers"].append({"tag": "another", "selector": ["proxy"]})
    source["routing"]["rules"].append({"balancerTag": "another"})
    with pytest.raises(SourceGroupError, match="однозначно"):
        update(source)
    source["routing"]["rules"] = [{"balancerTag": "missing"}]
    with pytest.raises(SourceGroupError, match="отсутствует"):
        update(source)


def test_duplicate_source_tags_and_dependent_nodes_are_not_silently_imported():
    with pytest.raises(SourceGroupError, match="повторяются"):
        update(xray_group(xray_server("proxy-1"), xray_server("proxy-1")))
    source = xray_group(xray_server("proxy-1"))
    source["outbounds"][0]["streamSettings"]["sockopt"] = {"dialerProxy": "other"}
    with pytest.raises(SourceGroupError, match="нет совместимых"):
        update(source)


def test_native_singbox_group_uses_referenced_leaves_and_our_policy():
    source = {
        "route": {"final": "auto"},
        "outbounds": [
            {"type": "direct", "tag": "direct"},
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": ["node"],
                "interval": "10s",
            },
            {
                "type": "vless",
                "tag": "node",
                "server": "node.test",
                "server_port": 443,
                "uuid": UUID,
            },
        ],
    }
    result = update(source)
    assert result.updated_config["outbounds"][0]["interval"] == "1m"
    assert result.updated_config["outbounds"][1]["server"] == "node.test"


def test_native_group_skips_unknown_type_and_invalid_authentication():
    good = {
        "type": "vless",
        "tag": "good",
        "server": "node.test",
        "server_port": 443,
        "uuid": UUID,
    }
    unknown = dict(good, tag="unknown", type="unsupported-proxy")
    invalid_auth = dict(good, tag="invalid", uuid="not-a-uuid")
    source = {
        "outbounds": [
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": ["good", "unknown", "invalid"],
            },
            {"type": "direct"},
            good,
            unknown,
            invalid_auth,
        ]
    }
    result = update(source)
    assert len(result.warnings) == 2
    assert len(result.updated_config["outbounds"][0]["outbounds"]) == 1


def test_missing_native_final_does_not_silently_choose_a_different_group():
    source = {
        "route": {"final": "missing"},
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": ["good"]},
            {
                "type": "vless",
                "tag": "good",
                "server": "node.test",
                "server_port": 443,
                "uuid": UUID,
            },
        ],
    }
    with pytest.raises(SourceGroupError, match="по умолчанию отсутствует"):
        update(source)


def test_malformed_type_in_one_member_is_skipped_not_a_type_error():
    malformed = {"tag": "proxy-bad", "type": ["vless"]}
    result = update(xray_group(xray_server("proxy-good"), malformed))
    assert len(result.warnings) == 1
    assert len(result.updated_config["outbounds"][0]["outbounds"]) == 1


def test_single_import_still_skips_a_malformed_first_outbound():
    result = update({"outbounds": [{"type": ["broken"]}, xray_server("single")]})
    assert result.updated_config["outbounds"][0]["type"] == "vless"
    assert len(result.updated_config["outbounds"]) == 2


def test_source_remarks_become_readable_group_tags_and_preview_names():
    source = xray_group(
        xray_server("proxy-de"), xray_server("proxy-nl"), xray_server("proxy-ee")
    )
    names = ["🇩🇪 Германия", "🇳🇱 Нидерланды", "🇪🇪 Эстония"]
    for item, name in zip(source["outbounds"], names, strict=True):
        item["remarks"] = name
    original = deepcopy(source)
    result = update(source)
    assert result.updated_config["outbounds"][0]["outbounds"] == names
    assert [item["tag"] for item in result.updated_config["outbounds"][1:-1]] == names
    assert [item["tag"] for item in result.preview["new_outbound"]["servers"]] == names
    assert all("remarks" not in item for item in result.updated_config["outbounds"])
    assert source == original
    assert not update(source, result.updated_config).has_changes


def test_name_fallback_and_uniqueness_include_group_and_unrelated_tags():
    source = xray_group(*(xray_server(f"proxy-{i}") for i in range(6)))
    for index in (0, 1):
        source["outbounds"][index]["remarks"] = "Германия"
    source["outbounds"][2].update(remarks=" ", remark="Нидерланды", name="not used")
    source["outbounds"][3].update(remarks=None, name="Эстония")
    source["outbounds"][4]["remarks"] = "router-proxy"
    source["outbounds"][5]["remarks"] = "direct"
    result = update(source).updated_config
    assert result["outbounds"][0]["outbounds"] == [
        "Германия",
        "Германия (2)",
        "Нидерланды",
        "Эстония",
        "router-proxy (2)",
        "direct (2)",
    ]
    assert result["outbounds"][-1] == router_config()["outbounds"][-1]


def test_skipped_node_does_not_shift_names_to_another_connection():
    source = xray_group(xray_server("proxy-bad", "xhttp"), xray_server("proxy-good"))
    source["outbounds"][0]["remarks"] = "Германия"
    source["outbounds"][1]["remarks"] = "Эстония"
    result = update(source).updated_config
    assert result["outbounds"][0]["outbounds"] == ["Эстония"]


def test_native_name_metadata_is_used_as_tag_and_not_sent_as_unknown_fields():
    source = {
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": ["node"]},
            {
                "type": "vless",
                "tag": "node",
                "server": "node.test",
                "server_port": 443,
                "uuid": UUID,
                "name": "🇪🇪 Эстония",
            },
        ]
    }
    result = update(source).updated_config
    assert result["outbounds"][1]["tag"] == "🇪🇪 Эстония"
    assert "name" not in result["outbounds"][1]


def legacy_router():
    router = router_config()
    tags = ["singroute-auto-18c12166eba3eee4-1", "singroute-auto-18c12166eba3eee4-2"]
    router["outbounds"][0] = {
        "type": "urltest",
        "tag": "router-proxy",
        "outbounds": tags,
        "url": "https://www.gstatic.com/generate_204",
        "interval": "1m",
        "tolerance": 20,
        "interrupt_exist_connections": True,
    }
    router["outbounds"][1:1] = [
        {
            "type": "vless",
            "tag": tag,
            "server": "legacy.test",
            "server_port": 443,
            "uuid": UUID,
        }
        for tag in tags
    ]
    return router


@pytest.mark.parametrize("automatic", [False, True])
def test_v0100_group_migrates_without_leaving_old_numbered_nodes(automatic):
    router = legacy_router()
    source = (
        xray_group(xray_server("proxy-new"))
        if automatic
        else {"outbounds": [xray_server("single")]}
    )
    result = update(source, router).updated_config
    assert "singroute-auto-" not in json.dumps(result)
    assert len(result["outbounds"]) == (3 if automatic else 2)
    assert result["outbounds"][-1] == router["outbounds"][-1]
    assert {key: value for key, value in result.items() if key != "outbounds"} == {
        key: value for key, value in router.items() if key != "outbounds"
    }


def test_legacy_shared_member_is_protected_during_name_migration():
    router = legacy_router()
    router["route"]["rules"].append({"outbound": router["outbounds"][1]["tag"]})
    with pytest.raises(ConfigPatchError, match="других настройках"):
        update(xray_group(xray_server("proxy-new")), router)


def test_names_cannot_inject_log_lines_or_terminal_controls():
    source = xray_group(xray_server("proxy-1"))
    source["outbounds"][0]["remarks"] = "Германия\r\nFAKE\x1b\x00\u202e"
    result = update(source).updated_config
    assert result["outbounds"][0]["outbounds"] == ["Германия  FAKE"]
