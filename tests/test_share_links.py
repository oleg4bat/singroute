import json
import traceback
from pathlib import Path

import pytest

from singroute.application.operation import (
    MAX_CONFIG_BYTES,
    parse_imported_outbound,
    prepare_config_update,
    validate_imported_config,
)
from singroute.core.errors import ConfigParseError, ConfigPatchError
from singroute.core.share_links import parse_share_link

UUID = "00000000-0000-4000-8000-000000000001"
BASE = f"vless://{UUID}@192.0.2.10:443"
WS_LINK = (
    BASE + "?encryption=none&type=ws&fp=firefox&sni=tls.example.test&flow="
    "&host=ws.example.test&path=/api/v1/picks/sample/queue&security=tls"
    "#%F0%9F%87%A9%F0%9F%87%AA%20%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80"
)
FIXTURES = Path(__file__).parent / "fixtures"


def test_happ_link_and_json_prepare_identical_router_outbound():
    source = (FIXTURES / "source_happ_vless_ws_tls.json").read_text(encoding="utf-8")
    router = json.loads(
        (FIXTURES / "router_base_singbox.json").read_text(encoding="utf-8")
    )
    from_link = prepare_config_update("\ufeff \n" + WS_LINK + "\n", json.dumps(router))
    from_json = prepare_config_update(source, json.dumps(router))

    assert from_link == from_json
    outbound = from_link.updated_config["outbounds"][0]
    assert outbound == {
        "type": "vless",
        "tag": router["outbounds"][0]["tag"],
        "server": "192.0.2.10",
        "server_port": 443,
        "uuid": UUID,
        "tls": {
            "enabled": True,
            "server_name": "tls.example.test",
            "insecure": False,
            "utls": {"enabled": True, "fingerprint": "firefox"},
        },
        "transport": {
            "type": "ws",
            "path": "/api/v1/picks/sample/queue",
            "headers": {"Host": "ws.example.test"},
        },
    }
    assert from_link.updated_config["outbounds"][1:] == router["outbounds"][1:]
    for key in router.keys() - {"outbounds"}:
        assert from_link.updated_config[key] == router[key]
    assert UUID not in json.dumps(from_link.preview)
    assert from_link.preview["new_outbound"]["uuid"] == "***"
    validate_imported_config(WS_LINK)


def test_link_preserves_ipv6_encoded_path_plus_alpn_and_tls_verification():
    result = parse_share_link(
        f"vless://{UUID}@[2001:db8::1]:8443?type=ws&security=tls&fp=firefox"
        "&sni=tls.example.test&host=ws.example.test"
        "&path=%2Fsocket%3Ftoken%3Da%2Bb%26other%3Dx%252Fy"
        "&alpn=http%2F1.1&allowInsecure=false#%D0%A2%D0%B5%D1%81%D1%82"
    )
    assert result["server"] == "2001:db8::1"
    assert result["server_port"] == 8443
    assert result["transport"]["path"] == "/socket?token=a+b&other=x%2Fy"
    assert result["tls"]["alpn"] == ["http/1.1"]
    assert not result["tls"]["insecure"]


def test_link_defaults_and_explicit_insecure_flag():
    plain = parse_share_link(BASE)
    assert "tls" not in plain and "transport" not in plain
    assert "network" not in plain
    tls = parse_share_link(BASE + "?type=ws&security=tls&insecure=1")
    assert tls["transport"] == {"type": "ws", "path": "/"}
    assert tls["tls"]["server_name"] == "192.0.2.10"
    assert tls["tls"]["utls"]["fingerprint"] == "chrome"
    assert tls["tls"]["insecure"]


@pytest.mark.parametrize("network", ["tcp", "raw", "grpc"])
def test_reality_link_imports_existing_transports_and_empty_short_id(network):
    result = parse_share_link(
        BASE + f"?type={network}&security=reality&sni=tls.example.test&fp=firefox"
        "&pbk=CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid="
        "&alpn=h2%2Chttp%2F1.1"
        + (
            "&serviceName=example&mode=multi"
            if network == "grpc"
            else "&flow=xtls-rprx-vision"
        )
    )
    assert result["tls"]["reality"]["enabled"]
    assert result["tls"]["reality"]["short_id"] == ""
    assert result["tls"]["alpn"] == ["h2", "http/1.1"]
    if network == "grpc":
        assert result["transport"] == {"type": "grpc", "service_name": "example"}
    else:
        assert result["flow"] == "xtls-rprx-vision"
        assert "transport" not in result


@pytest.mark.parametrize(
    "link",
    [
        "vless://secret@host:443",
        f"vless://{UUID}@host",
        f"vless://{UUID}@host:0",
        f"vless://{UUID}@host:65536",
        f"vless://{UUID}@host:private-port",
        f"vless://{UUID}@[broken:443",
        f"vless://{UUID}:secret@host:443",
        BASE + "/private-path?type=ws",
        BASE + "?type=ws&type=tcp",
        BASE + "?type=ws&path=%ZZ",
        BASE + "?type=ws&path=%FF",
        BASE + "?type=ws&host=private%0d%0aValue",
        BASE + "?type=xhttp&extra=private-value",
        BASE + "?type=ws&security=reality&pbk=private-key",
        BASE + "?type=ws&security=tls&flow=xtls-rprx-vision",
        BASE + "?security=tls&encryption=private-encryption",
        BASE + "?security=tls&fp=",
        BASE + "?security=tls&alpn=",
        BASE + "?security=tls&insecure=private-value",
        BASE + "?security=tls&insecure=0&allowInsecure=1",
        BASE + "?security=reality",
        BASE + "?security=private-security",
        BASE + "?type=grpc&mode=guna",
        BASE + "?type=grpc&authority=private-authority",
        BASE + "?headerType=http",
        BASE + "?unknown=private-value",
        BASE + "?type",
        BASE + "\n" + BASE,
        "vmess://private-value",
        "https://private-value",
    ],
)
def test_invalid_or_unsupported_links_fail_without_disclosing_values(link):
    with pytest.raises(ConfigPatchError) as raised:
        parse_imported_outbound(link)
    error_text = "".join(traceback.format_exception(raised.value))
    for value in (UUID, "secret@", "private-value", "private-key", "private-authority"):
        assert value not in error_text


def test_share_link_size_is_limited_before_parsing():
    with pytest.raises(ConfigParseError, match="safe limit"):
        validate_imported_config(BASE + "#" + "x" * MAX_CONFIG_BYTES)
