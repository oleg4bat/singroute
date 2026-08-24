import json

import pytest

from singroute.application.operation import prepare_config_update
from singroute.core.converters import normalize_outbound_to_singbox
from singroute.core.errors import ConfigPatchError


def test_singbox_outbound_is_returned_as_deep_copy():
    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": "example.com",
        "tls": {"reality": {"public_key": "public-key"}},
    }

    result = normalize_outbound_to_singbox(outbound)

    assert result == outbound
    assert result is not outbound
    assert result["tls"] is not outbound["tls"]
    result["tls"]["reality"]["public_key"] = "changed"
    assert outbound["tls"]["reality"]["public_key"] == "public-key"


def test_xray_vless_reality_tcp_converts_to_singbox_vless():
    result = normalize_outbound_to_singbox(_xray_vless_reality_outbound())

    assert result == {
        "type": "vless",
        "tag": "proxy",
        "server": "155.117.137.201",
        "server_port": 443,
        "uuid": "uuid",
        "flow": "xtls-rprx-vision",
        "network": "tcp",
        "tls": {
            "enabled": True,
            "server_name": "example.com",
            "insecure": False,
            "reality": {
                "enabled": True,
                "public_key": "public-key",
                "short_id": "short-id",
            },
            "utls": {"enabled": True, "fingerprint": "firefox"},
        },
    }


def test_xray_vless_reality_ignores_spider_x():
    result = normalize_outbound_to_singbox(_xray_vless_reality_outbound())

    assert "spiderX" not in json.dumps(result)


def test_xray_vless_reality_omits_absent_flow_and_fingerprint():
    outbound = _xray_vless_reality_outbound()
    del outbound["settings"]["vnext"][0]["users"][0]["flow"]
    del outbound["streamSettings"]["realitySettings"]["fingerprint"]

    result = normalize_outbound_to_singbox(outbound)

    assert "flow" not in result
    assert "utls" not in result["tls"]


def test_xray_hysteria_version_2_converts_to_singbox_hysteria2():
    result = normalize_outbound_to_singbox(_xray_hysteria2_outbound())

    assert result == {
        "type": "hysteria2",
        "tag": "proxy",
        "server": "mehceh2020store.ru",
        "server_port": 443,
        "password": "admin:password",
        "tls": {
            "enabled": True,
            "insecure": False,
            "server_name": "mehceh2020store.ru",
            "alpn": ["h3"],
        },
    }


def test_hysteria_version_1_raises_clear_error():
    outbound = _xray_hysteria2_outbound()
    outbound["settings"]["version"] = 1
    outbound["streamSettings"]["hysteriaSettings"]["version"] = 1

    with pytest.raises(ConfigPatchError, match="Only Hysteria2 is supported"):
        normalize_outbound_to_singbox(outbound)


def test_hysteria_unknown_version_raises_clear_error():
    outbound = _xray_hysteria2_outbound()
    del outbound["settings"]["version"]
    del outbound["streamSettings"]["hysteriaSettings"]["version"]

    with pytest.raises(ConfigPatchError, match="Only Hysteria2 is supported"):
        normalize_outbound_to_singbox(outbound)


def test_unsupported_protocol_raises_clear_error():
    with pytest.raises(ConfigPatchError, match="Unsupported outbound protocol: trojan"):
        normalize_outbound_to_singbox({"protocol": "trojan"})


def test_application_accepts_xray_exported_config_and_writes_singbox_outbound():
    exported_config_text = json.dumps(
        {
            "dns": {"servers": ["8.8.8.8"]},
            "inbounds": [{"tag": "mixed-in"}],
            "routing": {"rules": []},
            "policy": {"levels": {}},
            "metrics": {"tag": "metrics"},
            "remarks": "client export",
            "outbounds": [
                _xray_vless_reality_outbound(),
                {"protocol": "freedom", "tag": "must-not-copy"},
            ],
        }
    )
    router_config_text = json.dumps(
        {
            "dns": {"servers": ["1.1.1.1"]},
            "outbounds": [
                {"type": "direct", "tag": "router-proxy"},
                {"type": "block", "tag": "blocked"},
            ],
        }
    )

    result = prepare_config_update(exported_config_text, router_config_text)
    updated_config = result.updated_config
    outbound = updated_config["outbounds"][0]

    assert outbound["type"] == "vless"
    assert outbound["tag"] == "router-proxy"
    assert "protocol" not in outbound
    assert updated_config["dns"] == {"servers": ["1.1.1.1"]}
    assert "inbounds" not in updated_config
    assert updated_config["outbounds"][1] == {"type": "block", "tag": "blocked"}


def test_application_patch_preserves_old_router_tag():
    result = prepare_config_update(
        json.dumps({"outbounds": [_xray_hysteria2_outbound()]}),
        json.dumps({"outbounds": [{"type": "direct", "tag": "router-tag"}]}),
    )
    updated_config = result.updated_config

    assert updated_config["outbounds"][0]["type"] == "hysteria2"
    assert updated_config["outbounds"][0]["tag"] == "router-tag"


def _xray_vless_reality_outbound():
    return {
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "155.117.137.201",
                    "port": 443,
                    "users": [
                        {
                            "id": "uuid",
                            "flow": "xtls-rprx-vision",
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "allowInsecure": False,
                "fingerprint": "firefox",
                "publicKey": "public-key",
                "serverName": "example.com",
                "shortId": "short-id",
                "spiderX": "/",
            },
            "tcpSettings": {"header": {"type": "none"}},
        },
        "tag": "proxy",
    }


def _xray_hysteria2_outbound():
    return {
        "protocol": "hysteria",
        "settings": {
            "address": "mehceh2020store.ru",
            "port": 443,
            "version": 2,
        },
        "streamSettings": {
            "network": "hysteria",
            "security": "tls",
            "hysteriaSettings": {
                "auth": "admin:password",
                "version": 2,
            },
            "tlsSettings": {
                "allowInsecure": False,
                "alpn": ["h3"],
                "serverName": "mehceh2020store.ru",
            },
        },
        "tag": "proxy",
    }
