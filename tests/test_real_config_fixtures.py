import json
from pathlib import Path

from singroute.application.operation import prepare_config_update

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_source_singbox_vless_fixture_patches_router_config():
    router_base = json.loads(load_fixture("router_base_singbox.json"))

    result = prepare_config_update(
        load_fixture("source_singbox_vless.json"),
        json.dumps(router_base),
    )
    updated_config = result.updated_config
    outbound = updated_config["outbounds"][0]

    assert outbound["type"] == "vless"
    assert outbound["server"] == "source-singbox-vless.example.test"
    assert outbound["server_port"] == 8443
    assert outbound["tag"] == router_base["outbounds"][0]["tag"]
    assert_no_xray_fields(outbound)
    assert_router_sections_preserved(updated_config, router_base)
    assert_router_extra_outbounds_preserved(updated_config, router_base)


def test_source_happ_vless_reality_1_fixture_patches_router_config():
    router_base = json.loads(load_fixture("router_base_singbox.json"))

    result = prepare_config_update(
        load_fixture("source_happ_vless_reality_1.json"),
        json.dumps(router_base),
    )
    updated_config = result.updated_config
    outbound = updated_config["outbounds"][0]

    assert outbound["type"] == "vless"
    assert outbound["server"] == "happ-vless-1.example.test"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "TEST-HAPP-VLESS-UUID-1"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["network"] == "tcp"
    assert outbound["tag"] == router_base["outbounds"][0]["tag"]
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "reality-1.example.test"
    assert outbound["tls"]["reality"]["enabled"] is True
    assert outbound["tls"]["reality"]["public_key"] == "TEST-HAPP-PUBLIC-KEY-1"
    assert outbound["tls"]["reality"]["short_id"] == "TESTSHORTID1"
    assert "spiderX" not in json.dumps(outbound)
    assert_no_xray_fields(outbound)


def test_source_happ_vless_reality_2_fixture_does_not_copy_source_sections():
    router_base = json.loads(load_fixture("router_base_singbox.json"))

    result = prepare_config_update(
        load_fixture("source_happ_vless_reality_2.json"),
        json.dumps(router_base),
    )
    updated_config = result.updated_config
    outbound = updated_config["outbounds"][0]

    assert outbound["type"] == "vless"
    assert outbound["server"] == "happ-vless-2.example.test"
    assert outbound["server_port"] == 9443
    assert outbound["uuid"] == "TEST-HAPP-VLESS-UUID-2"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["network"] == "tcp"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "reality-2.example.test"
    assert outbound["tls"]["reality"]["enabled"] is True
    assert outbound["tls"]["reality"]["public_key"] == "TEST-HAPP-PUBLIC-KEY-2"
    assert outbound["tls"]["reality"]["short_id"] == "TESTSHORTID2"
    assert_no_xray_fields(outbound)
    assert_router_sections_preserved(updated_config, router_base)
    assert_router_extra_outbounds_preserved(updated_config, router_base)
    assert updated_config["dns"] == router_base["dns"]
    assert updated_config["inbounds"] == router_base["inbounds"]
    assert updated_config["route"] == router_base["route"]
    assert updated_config["routing"] == router_base["routing"]
    assert "source-direct" not in json.dumps(updated_config)
    assert "source-block" not in json.dumps(updated_config)
    assert "source-socks" not in json.dumps(updated_config)
    assert "source-metrics" not in json.dumps(updated_config)


def test_source_happ_hysteria2_fixture_patches_router_config():
    router_base = json.loads(load_fixture("router_base_singbox.json"))

    result = prepare_config_update(
        load_fixture("source_happ_hysteria2.json"),
        json.dumps(router_base),
    )
    updated_config = result.updated_config
    outbound = updated_config["outbounds"][0]

    assert outbound["type"] == "hysteria2"
    assert outbound["server"] == "hysteria2.example.test"
    assert outbound["server_port"] == 443
    assert outbound["password"] == "TEST-HYSTERIA2-AUTH-PASSWORD"
    assert outbound["tag"] == router_base["outbounds"][0]["tag"]
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "hysteria2.example.test"
    assert outbound["tls"]["alpn"] == ["h3"]
    assert outbound["tls"]["insecure"] is False
    assert_no_xray_fields(outbound)


def test_fixture_summaries_do_not_reveal_secrets():
    result = prepare_config_update(
        load_fixture("source_happ_vless_reality_1.json"),
        load_fixture("router_base_singbox.json"),
    )
    summaries_text = json.dumps(result.preview, ensure_ascii=False)

    for secret in [
        "TEST-HAPP-VLESS-UUID-1",
        "TEST-OLD-ROUTER-UUID",
        "TEST-OLD-ROUTER-PASSWORD",
        "TEST-HAPP-PUBLIC-KEY-1",
        "TESTSHORTID1",
        "TEST-OLD-PRIVATE-KEY",
    ]:
        assert secret not in summaries_text

    old_summary = result.preview["old_outbound"]
    new_summary = result.preview["new_outbound"]
    assert old_summary["uuid"] == "***"
    assert old_summary["password"] == "***"
    assert old_summary["tls"]["private_key"] == "***"
    assert new_summary["uuid"] == "***"
    assert new_summary["tls"]["reality"]["public_key"] == "***"
    assert new_summary["tls"]["reality"]["short_id"] == "***"


def test_hysteria_fixture_summary_masks_auth_as_password():
    result = prepare_config_update(
        load_fixture("source_happ_hysteria2.json"),
        load_fixture("router_base_singbox.json"),
    )
    summaries_text = json.dumps(result.preview, ensure_ascii=False)

    assert "TEST-HYSTERIA2-AUTH-PASSWORD" not in summaries_text
    new_summary = result.preview["new_outbound"]
    assert "auth" not in new_summary
    assert new_summary["password"] == "***"


def assert_no_xray_fields(outbound):
    assert "protocol" not in outbound
    assert "settings" not in outbound
    assert "streamSettings" not in outbound


def assert_router_sections_preserved(result, router_base):
    for section in ["log", "dns", "inbounds", "route", "routing"]:
        assert result[section] == router_base[section]


def assert_router_extra_outbounds_preserved(result, router_base):
    assert result["outbounds"][1:] == router_base["outbounds"][1:]


def load_fixture(name):
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")
