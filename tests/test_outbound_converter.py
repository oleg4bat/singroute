import json
from pathlib import Path

import pytest

from singroute.application.operation import prepare_config_update
from singroute.core.converters import normalize_outbound_to_singbox
from singroute.core.errors import (
    ConfigPatchError,
    UnsupportedGrpcSettingError,
    UnsupportedVlessTransportError,
)

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_xray_vless_reality_grpc_preserves_service_name():
    outbound = _xray_vless_reality_outbound()
    outbound["streamSettings"]["network"] = "grpc"
    outbound["streamSettings"]["grpcSettings"] = {
        "serviceName": "test-service",
        "multiMode": False,
        "authority": "",
    }
    del outbound["settings"]["vnext"][0]["users"][0]["flow"]

    result = normalize_outbound_to_singbox(outbound)

    assert result["transport"] == {"type": "grpc", "service_name": "test-service"}
    assert "network" not in result
    assert "packet_encoding" not in result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority", "private.example.test"),
        ("user_agent", "private-agent"),
    ],
)
def test_xray_vless_grpc_rejects_unrepresentable_settings(field, value):
    outbound = _xray_vless_reality_outbound()
    outbound["streamSettings"]["network"] = "grpc"
    outbound["streamSettings"]["grpcSettings"] = {
        "serviceName": "test-service",
        field: value,
    }

    with pytest.raises(ConfigPatchError, match="Unsupported gRPC setting") as raised:
        normalize_outbound_to_singbox(outbound)

    assert str(value) not in str(raised.value)


def test_xray_vless_grpc_rejects_invalid_multi_mode_type():
    outbound = _xray_vless_reality_outbound()
    outbound["streamSettings"]["network"] = "grpc"
    outbound["streamSettings"]["grpcSettings"] = {"multiMode": "false"}

    with pytest.raises(ConfigPatchError, match=r"grpcSettings\.multiMode"):
        normalize_outbound_to_singbox(outbound)


def test_xray_grpc_rejects_multi_mode_when_path_or_security_is_not_direct_reality():
    outbound = _xray_vless_reality_outbound()
    stream_settings = outbound["streamSettings"]
    stream_settings["network"] = "grpc"
    stream_settings["grpcSettings"] = {
        "serviceName": "/custom/TunMulti",
        "multiMode": True,
    }

    with pytest.raises(UnsupportedGrpcSettingError, match="multiMode"):
        normalize_outbound_to_singbox(outbound)

    outbound = _xray_trojan_outbound()
    stream_settings = outbound["streamSettings"]
    stream_settings["network"] = "grpc"
    stream_settings["grpcSettings"] = {
        "serviceName": "ordinary-service",
        "multiMode": True,
    }
    with pytest.raises(UnsupportedGrpcSettingError, match="multiMode"):
        normalize_outbound_to_singbox(outbound)


def test_anonymized_happ_grpc_export_converts_multi_mode_to_regular_grpc():
    source = json.loads(
        (FIXTURES / "source_happ_vless_reality_grpc.json").read_text(encoding="utf-8")
    )
    outbound = source["outbounds"][0]

    result = normalize_outbound_to_singbox(outbound)

    assert result["transport"] == {
        "type": "grpc",
        "service_name": "sample-grpc-service",
    }
    assert source["outbounds"][0]["streamSettings"]["grpcSettings"]["multiMode"]


def test_anonymized_happ_grpc_export_prepares_router_update_with_multi_mode():
    source = json.loads(
        (FIXTURES / "source_happ_vless_reality_grpc.json").read_text(encoding="utf-8")
    )
    result = prepare_config_update(
        json.dumps(source),
        json.dumps({"outbounds": [{"type": "direct", "tag": "router-proxy"}]}),
    ).updated_config["outbounds"][0]

    assert result["type"] == "vless"
    assert result["tag"] == "router-proxy"
    assert result["transport"] == {
        "type": "grpc",
        "service_name": "sample-grpc-service",
    }
    assert result["tls"]["utls"] == {"enabled": True, "fingerprint": "qq"}
    assert "routing" not in result


def test_unknown_vless_transport_value_is_not_stored_in_error():
    outbound = _xray_vless_reality_outbound()
    outbound["streamSettings"]["network"] = "private-network-value"

    with pytest.raises(UnsupportedVlessTransportError) as raised:
        normalize_outbound_to_singbox(outbound)

    assert raised.value.network is None
    assert "private-network-value" not in str(raised.value)


def test_xray_vless_reality_omits_absent_flow_and_fingerprint():
    outbound = _xray_vless_reality_outbound()
    del outbound["settings"]["vnext"][0]["users"][0]["flow"]
    del outbound["streamSettings"]["realitySettings"]["fingerprint"]

    result = normalize_outbound_to_singbox(outbound)

    assert "flow" not in result
    assert "utls" not in result["tls"]


def test_xray_vless_reality_rejects_string_allow_insecure():
    outbound = _xray_vless_reality_outbound()
    outbound["streamSettings"]["realitySettings"]["allowInsecure"] = "false"

    with pytest.raises(ConfigPatchError, match=r"realitySettings\.allowInsecure"):
        normalize_outbound_to_singbox(outbound)


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


def test_xray_hysteria_rejects_non_boolean_allow_insecure():
    outbound = _xray_hysteria2_outbound()
    outbound["streamSettings"]["tlsSettings"]["allowInsecure"] = 0

    with pytest.raises(ConfigPatchError, match=r"tlsSettings\.allowInsecure"):
        normalize_outbound_to_singbox(outbound)


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


def test_xray_trojan_tls_converts_to_singbox():
    result = normalize_outbound_to_singbox(_xray_trojan_outbound())

    assert result == {
        "type": "trojan",
        "tag": "proxy",
        "server": "trojan.example.test",
        "server_port": 443,
        "password": "test-password",
        "tls": {
            "enabled": True,
            "insecure": False,
            "server_name": "sni.example.test",
            "alpn": ["h2", "http/1.1"],
            "utls": {"enabled": True, "fingerprint": "firefox"},
        },
    }


def test_anonymized_happ_trojan_export_patches_only_router_server():
    source = (FIXTURES / "source_happ_trojan_tls.json").read_text(encoding="utf-8")
    router = {
        "dns": {"servers": ["1.1.1.1"]},
        "outbounds": [
            {"type": "vless", "tag": "router-proxy"},
            {"type": "direct", "tag": "direct"},
        ],
    }

    result = prepare_config_update(source, json.dumps(router)).updated_config

    assert result["outbounds"][0] == {
        "type": "trojan",
        "tag": "router-proxy",
        "server": "198.51.100.10",
        "server_port": 7443,
        "password": "TEST-TROJAN-PASSWORD",
        "tls": {
            "enabled": True,
            "server_name": "trojan.example.test",
            "insecure": False,
            "utls": {"enabled": True, "fingerprint": "firefox"},
        },
    }
    assert result["dns"] == router["dns"]
    assert result["outbounds"][1] == router["outbounds"][1]
    assert "remarks" not in result["outbounds"][0]


def test_xray_trojan_direct_settings_and_grpc_reality():
    outbound = _xray_trojan_outbound()
    outbound["settings"] = outbound["settings"]["servers"][0]
    outbound["streamSettings"] = {
        "network": "grpc",
        "security": "reality",
        "grpcSettings": {"serviceName": "trojan-grpc"},
        "realitySettings": {
            "serverName": "sni.example.test",
            "publicKey": "public-key",
            "shortId": "01234567",
        },
    }

    result = normalize_outbound_to_singbox(outbound)

    assert result["type"] == "trojan"
    assert result["transport"] == {"type": "grpc", "service_name": "trojan-grpc"}
    assert result["tls"]["reality"]["public_key"] == "public-key"


def test_xray_trojan_rejects_unsupported_transport_and_security():
    outbound = _xray_trojan_outbound()
    outbound["streamSettings"]["network"] = "ws"
    with pytest.raises(ConfigPatchError, match="Unsupported Trojan transport"):
        normalize_outbound_to_singbox(outbound)

    outbound["streamSettings"]["network"] = "tcp"
    outbound["streamSettings"]["security"] = "none"
    with pytest.raises(ConfigPatchError, match="only TLS or Reality"):
        normalize_outbound_to_singbox(outbound)


def test_xray_trojan_rejects_connection_settings_it_cannot_preserve():
    outbound = _xray_trojan_outbound()
    outbound["streamSettings"]["tcpSettings"]["header"] = {"type": "http"}
    with pytest.raises(ConfigPatchError, match="Unsupported Trojan TCP header"):
        normalize_outbound_to_singbox(outbound)

    outbound = _xray_trojan_outbound()
    outbound["streamSettings"]["tlsSettings"]["pinnedPeerCertSha256"] = "secret"
    with pytest.raises(
        ConfigPatchError, match="Unsupported Trojan TLS setting"
    ) as raised:
        normalize_outbound_to_singbox(outbound)
    assert "secret" not in str(raised.value)


def test_unsupported_protocol_raises_clear_error():
    with pytest.raises(ConfigPatchError, match="Unsupported outbound protocol: vmess"):
        normalize_outbound_to_singbox({"protocol": "vmess"})


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


def test_application_skips_malformed_candidate_and_converts_next_outbound():
    malformed = _xray_vless_reality_outbound()
    malformed["settings"] = {}

    result = prepare_config_update(
        json.dumps({"outbounds": [malformed, _xray_hysteria2_outbound()]}),
        json.dumps({"outbounds": [{"type": "direct", "tag": "router-tag"}]}),
    )

    outbound = result.updated_config["outbounds"][0]
    assert outbound["type"] == "hysteria2"
    assert outbound["server"] == "mehceh2020store.ru"
    assert outbound["tag"] == "router-tag"


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


def _xray_trojan_outbound():
    return {
        "protocol": "trojan",
        "settings": {
            "servers": [
                {
                    "address": "trojan.example.test",
                    "port": 443,
                    "password": "test-password",
                    "email": "source-only@example.test",
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "sni.example.test",
                "allowInsecure": False,
                "alpn": ["h2", "http/1.1"],
                "fingerprint": "firefox",
            },
            "tcpSettings": {"header": {"type": "none"}},
        },
        "tag": "proxy",
    }
