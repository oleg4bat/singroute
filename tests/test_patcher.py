import pytest

from singroute.core.errors import ConfigPatchError
from singroute.core.patcher import (
    patch_router_config,
    select_exported_outbound,
)


def test_outbounds_first_item_is_replaced():
    router_config = {
        "log": {"level": "info"},
        "outbounds": [
            {"type": "direct", "tag": "old"},
            {"type": "block", "tag": "blocked"},
        ],
    }
    new_outbound = {"type": "vless", "server": "example.com", "tag": "new"}

    result = patch_router_config(router_config, new_outbound)

    assert result["outbounds"][0] == {
        "type": "vless",
        "server": "example.com",
        "tag": "old",
    }
    assert result["outbounds"][1] == {"type": "block", "tag": "blocked"}


def test_other_router_config_sections_are_preserved():
    router_config = {
        "log": {"level": "debug"},
        "dns": {"servers": [{"tag": "cloudflare", "address": "1.1.1.1"}]},
        "route": {"rules": [{"outbound": "proxy"}]},
        "outbounds": [{"type": "direct", "tag": "proxy"}],
    }
    new_outbound = {"type": "trojan", "server": "vpn.example"}

    result = patch_router_config(router_config, new_outbound)

    assert result["log"] == router_config["log"]
    assert result["dns"] == router_config["dns"]
    assert result["route"] == router_config["route"]


def test_old_tag_is_preserved():
    router_config = {"outbounds": [{"type": "direct", "tag": "router-proxy"}]}
    new_outbound = {"type": "vless", "tag": "exported-proxy", "server": "example.com"}

    result = patch_router_config(router_config, new_outbound)

    assert result["outbounds"][0]["tag"] == "router-proxy"
    assert result["outbounds"][0]["type"] == "vless"
    assert result["outbounds"][0]["server"] == "example.com"


def test_router_outbounds_with_invalid_type_raises_clear_error():
    router_config = {"outbounds": {"type": "direct"}}
    new_outbound = {"type": "vless", "server": "example.com"}

    with pytest.raises(ConfigPatchError, match=r'router_config\["outbounds"\].*list'):
        patch_router_config(router_config, new_outbound)


def test_empty_exported_outbounds_raises_clear_error():
    with pytest.raises(ConfigPatchError, match="не должен быть пустым"):
        select_exported_outbound({"outbounds": []})


def test_non_dict_first_exported_outbound_raises_clear_error():
    with pytest.raises(ConfigPatchError, match=r"должен быть объектом JSON"):
        select_exported_outbound({"outbounds": ["not-an-outbound"]})


def test_select_exported_outbound_skips_direct_and_selects_vless():
    result = select_exported_outbound(
        {
            "outbounds": [
                {"type": "direct", "tag": "source-direct"},
                {"type": "vless", "tag": "source-proxy", "server": "vpn.test"},
            ]
        }
    )

    assert result == {"type": "vless", "tag": "source-proxy", "server": "vpn.test"}


def test_select_exported_outbound_skips_block_and_selects_hysteria2():
    result = select_exported_outbound(
        {
            "outbounds": [
                {"type": "block", "tag": "source-block"},
                {"type": "hysteria2", "tag": "source-proxy", "server": "vpn.test"},
            ]
        }
    )

    assert result == {
        "type": "hysteria2",
        "tag": "source-proxy",
        "server": "vpn.test",
    }


def test_select_exported_outbound_skips_malformed_vless_candidate():
    result = select_exported_outbound(
        {
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {},
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                    },
                },
                {"type": "hysteria2", "server": "vpn.test"},
            ]
        }
    )

    assert result == {"type": "hysteria2", "server": "vpn.test"}


def test_select_exported_outbound_skips_malformed_hysteria2_candidate():
    result = select_exported_outbound(
        {
            "outbounds": [
                {
                    "protocol": "hysteria",
                    "settings": {"address": "broken.test", "port": 443, "version": 2},
                    "streamSettings": {"hysteriaSettings": {"version": 2}},
                },
                {"type": "vless", "server": "vpn.test"},
            ]
        }
    )

    assert result == {"type": "vless", "server": "vpn.test"}


def test_select_exported_outbound_reports_error_when_all_candidates_are_malformed():
    with pytest.raises(ConfigPatchError, match=r"settings\.vnext"):
        select_exported_outbound(
            {
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {},
                        "streamSettings": {
                            "network": "tcp",
                            "security": "reality",
                        },
                    }
                ]
            }
        )


def test_select_exported_outbound_without_supported_proxy_raises_clear_error():
    with pytest.raises(ConfigPatchError, match="поддерживаемый proxy outbound"):
        select_exported_outbound(
            {
                "outbounds": [
                    {"type": "direct", "tag": "source-direct"},
                    {"type": "block", "tag": "source-block"},
                    {"type": "dns", "tag": "source-dns"},
                    {"type": "selector", "tag": "source-selector"},
                    {"type": "urltest", "tag": "source-urltest"},
                ]
            }
        )
