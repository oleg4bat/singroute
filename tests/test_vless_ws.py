import json
from copy import deepcopy
from pathlib import Path

import pytest

from singroute.core.converters import normalize_outbound_to_singbox
from singroute.core.errors import ConfigPatchError

FIXTURE = Path(__file__).parent / "fixtures/source_happ_vless_ws_tls.json"


def ws_outbound():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["outbounds"][0]


def test_nested_and_flat_happ_ws_exports_convert_identically_without_mutation():
    flat = ws_outbound()
    nested = deepcopy(flat)
    settings = nested["settings"]
    nested["settings"] = {
        "vnext": [
            {
                "address": settings["address"],
                "port": settings["port"],
                "users": [{"id": settings["id"], "encryption": "none", "flow": ""}],
            }
        ]
    }
    before = deepcopy(nested)
    assert normalize_outbound_to_singbox(flat) == normalize_outbound_to_singbox(nested)
    assert nested == before


def test_ws_preserves_extra_headers_and_explicit_host_takes_priority():
    outbound = ws_outbound()
    settings = outbound["streamSettings"]["wsSettings"]
    settings["host"] = "override.example.test"
    settings["headers"] = {
        "host": "old.example.test",
        "User-Agent": "sample",
        "Authorization": "secret",
    }

    result = normalize_outbound_to_singbox(outbound)

    assert result["transport"]["headers"] == {
        "Host": "override.example.test",
        "User-Agent": "sample",
        "Authorization": "secret",
    }
    assert settings["headers"]["host"] == "old.example.test"
    assert result["tls"]["server_name"] == "tls.example.test"


@pytest.mark.parametrize(
    "path", ["/socket?ed=2560", "/socket?x=a%2Bb&ed=2560&y=c", "/socket?%65d=2560"]
)
def test_ws_early_data_preserves_other_query_parameters(path):
    outbound = ws_outbound()
    outbound["streamSettings"]["wsSettings"]["path"] = path
    result = normalize_outbound_to_singbox(outbound)["transport"]
    assert result["path"] == ("/socket?x=a%2Bb&y=c" if "x=" in path else "/socket")
    assert result["max_early_data"] == 2560
    assert result["early_data_header_name"] == "Sec-WebSocket-Protocol"


def test_ws_without_tls_and_default_path_does_not_restrict_udp():
    outbound = ws_outbound()
    outbound["streamSettings"] = {"network": "ws", "security": "none", "wsSettings": {}}
    result = normalize_outbound_to_singbox(outbound)
    assert result["transport"] == {"type": "ws", "path": "/"}
    assert "tls" not in result
    assert "network" not in result


def test_native_singbox_ws_export_keeps_existing_import_behavior():
    native = normalize_outbound_to_singbox(ws_outbound())
    before = deepcopy(native)
    result = normalize_outbound_to_singbox(native)
    assert result == before
    result["transport"]["headers"]["Host"] = "changed.example.test"
    assert native == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("path", 0),
        ("path", []),
        ("path", "private-path"),
        ("path", "/private\r\npath"),
        ("path", "/socket?ed=invalid"),
        ("path", "/socket?ed=2560&ed=512"),
        ("path", "/socket?ed=8193"),
        ("path", "/socket?ed=" + "1" * 5000),
        ("headers", {"Host": ["private-value"]}),
        ("headers", {"Host": "private\r\nvalue"}),
        ("headers", {"Invalid Name": "private-value"}),
        ("headers", {"Host": "a", "host": "b"}),
        ("host", "private\r\nvalue"),
        ("host", 1),
        ("heartbeatPeriod", 10),
        ("unknown", "private-value"),
    ],
)
def test_ws_rejects_invalid_or_unrepresentable_settings_without_values(key, value):
    outbound = ws_outbound()
    outbound["streamSettings"]["wsSettings"][key] = value
    with pytest.raises(ConfigPatchError) as raised:
        normalize_outbound_to_singbox(outbound)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("security", ["tls", "none"])
def test_vless_tcp_and_grpc_accept_standard_security(security):
    outbound = ws_outbound()
    for network in ("tcp", "grpc"):
        stream = outbound["streamSettings"]
        stream.update(
            network=network, security=security, grpcSettings={"serviceName": "sample"}
        )
        result = normalize_outbound_to_singbox(outbound)
        assert ("tls" in result) == (security == "tls")
        if network == "grpc":
            assert result["transport"] == {"type": "grpc", "service_name": "sample"}


def test_vless_tls_rejects_certificate_settings_instead_of_discarding_them():
    outbound = ws_outbound()
    outbound["streamSettings"]["tlsSettings"]["pinnedPeerCertSha256"] = ["private-pin"]
    with pytest.raises(ConfigPatchError, match="Unsupported TLS setting") as raised:
        normalize_outbound_to_singbox(outbound)
    assert "private-pin" not in str(raised.value)
