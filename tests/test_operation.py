import json

import pytest

from singroute.application.operation import (
    MAX_CONFIG_BYTES,
    parse_imported_selection,
    prepare_config_update,
    summarize_router_outbound,
)
from singroute.core.errors import ConfigParseError, ConfigPatchError
from singroute.core.preview import summarize_outbound


def test_summarize_router_outbound_masks_current_router_secrets():
    summary = summarize_router_outbound(
        json.dumps(
            {
                "outbounds": [
                    {
                        "type": "hysteria2",
                        "tag": "proxy",
                        "server": "current.test",
                        "password": "router-secret",
                    }
                ]
            }
        )
    )

    assert summary == {
        "type": "hysteria2",
        "tag": "proxy",
        "server": "current.test",
        "password": "***",
    }


def test_router_preview_uses_stable_order_and_masks_secrets():
    summary = summarize_router_outbound(
        json.dumps(
            {
                "outbounds": [
                    {
                        "tls": {
                            "reality": {"short_id": "hidden", "enabled": True},
                            "server_name": "example.test",
                            "enabled": True,
                        },
                        "uuid": "hidden",
                        "server": "vpn.test",
                        "tag": "proxy",
                        "type": "vless",
                    }
                ]
            }
        )
    )

    assert list(summary) == ["type", "tag", "server", "uuid", "tls"]
    assert list(summary["tls"]) == ["enabled", "server_name", "reality"]
    assert summary["uuid"] == "***"
    assert summary["tls"]["reality"]["short_id"] == "***"


def test_summarize_masks_case_and_extended_secret_names_recursively():
    summary = summarize_router_outbound(
        json.dumps(
            {
                "outbounds": [
                    {
                        "tag": "proxy",
                        "Token": "one",
                        "client-secret": "two",
                        "nested": {
                            "preSharedKey": "three",
                            "proxy_password_value": "four",
                        },
                    }
                ]
            }
        )
    )

    assert summary == {
        "tag": "proxy",
        "Token": "***",
        "client-secret": "***",
        "nested": {
            "preSharedKey": "***",
            "proxy_password_value": "***",
        },
    }


def test_prepare_config_update_accepts_imported_and_router_config_strings():
    imported_config_content = json.dumps(
        {"outbounds": [{"type": "vless", "tag": "imported", "server": "vpn.test"}]}
    )
    current_router_config_content = json.dumps(
        {
            "dns": {"servers": ["1.1.1.1"]},
            "outbounds": [{"type": "hysteria2", "tag": "router", "server": "old.test"}],
        }
    )

    result = prepare_config_update(
        imported_config_content,
        current_router_config_content,
    )

    assert result.updated_config["dns"] == {"servers": ["1.1.1.1"]}
    assert result.updated_config["outbounds"][0] == {
        "type": "vless",
        "tag": "router",
        "server": "vpn.test",
    }


def test_prepare_config_update_preview_contains_old_and_new_outbound_summaries():
    result = prepare_config_update(
        json.dumps(
            {
                "outbounds": [
                    {"type": "hysteria2", "tag": "imported", "server": "new.test"}
                ]
            }
        ),
        json.dumps(
            {"outbounds": [{"type": "vless", "tag": "router", "server": "old.test"}]}
        ),
    )

    assert result.preview["old_outbound"] == {
        "type": "vless",
        "tag": "router",
        "server": "old.test",
    }
    assert result.preview["new_outbound"] == {
        "type": "hysteria2",
        "tag": "router",
        "server": "new.test",
    }


def test_prepare_config_update_detects_secret_only_change_before_masking():
    result = prepare_config_update(
        json.dumps(
            {
                "outbounds": [
                    {
                        "type": "hysteria2",
                        "tag": "imported",
                        "server": "vpn.test",
                        "password": "new-secret",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "outbounds": [
                    {
                        "type": "hysteria2",
                        "tag": "router",
                        "server": "vpn.test",
                        "password": "old-secret",
                    }
                ]
            }
        ),
    )

    assert result.preview["old_outbound"] == result.preview["new_outbound"]
    assert result.has_changes is True


def test_prepare_config_update_reports_identical_outbound_as_unchanged():
    outbound = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": "vpn.test",
        "password": "same-secret",
    }

    result = prepare_config_update(
        json.dumps({"outbounds": [outbound]}),
        json.dumps({"outbounds": [outbound]}),
    )

    assert result.has_changes is False


def test_summarize_outbound_returns_independent_nested_containers():
    outbound = {
        "type": "vless",
        "server": "vpn.test",
        "uuid": "secret",
        "tls": {"alpn": ["h2"]},
    }

    summary = summarize_outbound(outbound)
    summary["tls"]["alpn"].append("h3")

    assert outbound["tls"]["alpn"] == ["h2"]
    assert summary["uuid"] == "***"


def test_prepare_config_update_wraps_invalid_imported_json():
    with pytest.raises(
        ConfigParseError,
        match="imported_config contains invalid JSON",
    ):
        prepare_config_update("{not-json", "{}")


def test_invalid_imported_json_retains_original_line_numbers():
    with pytest.raises(ConfigParseError, match="line 3"):
        prepare_config_update('\n\n{"outbounds": [}', "{}")


@pytest.mark.parametrize(
    "engine,separator,copies",
    [("sing-box", "", 2), ("xray", "", 2), ("sing-box", " ", 3), ("xray", "\r\n\t", 3)],
)
def test_imports_verbatim_repeated_json_once(engine, separator, copies):
    source = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "proxy",
                    "server": "vpn.test",
                    "server_port": 443,
                    "uuid": "11111111-1111-1111-1111-111111111111",
                }
            ],
            "remarks": 'Скобки внутри строки: } {, кавычки: "',
        },
        ensure_ascii=False,
        indent=2,
    )
    expected = parse_imported_selection(source, engine)

    selection = parse_imported_selection(
        "\ufeff \n" + separator.join([source] * copies) + "\r\n", engine
    )

    assert selection.servers == expected.servers
    assert len(selection.servers) == 1
    assert selection.warnings == (
        "Конфиг повторялся несколько раз. Импортирована одна копия.",
    )
    assert "11111111-1111-1111-1111-111111111111" not in str(selection.warnings)


def test_repeated_json_preserves_group_import_warnings():
    from config_samples import xray_group, xray_server

    source = json.dumps(
        xray_group(xray_server("proxy-good"), xray_server("proxy-bad", "xhttp"))
    )
    expected = parse_imported_selection(source)

    selection = parse_imported_selection(source + " " + source)

    assert selection.servers == expected.servers
    assert selection.automatic is True
    assert selection.names == expected.names
    assert expected.warnings
    assert selection.warnings[1:] == expected.warnings


@pytest.mark.parametrize(
    "tail,valid_copies",
    [
        ('{"outbounds": [{"type": "vless", "server": "other.test"}]}', 1),
        ('{"outbounds": [', 2),
        ("trailing text", 1),
        ("[]", 1),
        ("null", 1),
        ("// comment", 1),
        ('\ufeff{"outbounds": [{"type": "vless", "server": "vpn.test"}]}', 1),
    ],
)
def test_repeated_json_never_discards_different_or_damaged_tail(tail, valid_copies):
    source = '{"outbounds": [{"type": "vless", "server": "vpn.test"}]}'
    content = "\n".join([source] * valid_copies + [tail])

    with pytest.raises(ConfigParseError) as caught:
        parse_imported_selection(content)

    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert caught.value.__cause__.lineno == 2
    assert "vpn.test" not in str(caught.value)
    assert "other.test" not in str(caught.value)


def test_repeated_json_is_rejected_when_only_parsed_values_appear_equal():
    source = '{"outbounds": [{"type": "vless"}], "setting": 1}'
    changed = source.replace('"setting": 1', '"setting": true')
    assert json.loads(source) == json.loads(changed)

    with pytest.raises(ConfigParseError):
        parse_imported_selection(source + changed)


def test_repeated_json_keeps_import_size_limit(monkeypatch):
    source = '{"outbounds": [{"type": "vless"}]}'
    monkeypatch.setattr("singroute.application.operation.MAX_CONFIG_BYTES", len(source))

    with pytest.raises(ConfigParseError, match="safe limit"):
        parse_imported_selection(source + source)


def test_repeated_json_keeps_import_depth_limit(monkeypatch):
    source = '{"outbounds": [{"type": "vless"}]}'
    monkeypatch.setattr("singroute.application.operation.MAX_JSON_DEPTH", 2)

    with pytest.raises(ConfigParseError, match="nested"):
        parse_imported_selection(source + source)


def test_repeated_import_reaches_config_update_with_notice():
    source = '{"outbounds": [{"type": "vless", "server": "vpn.test"}]}'
    router = json.dumps({"outbounds": [{"type": "direct", "tag": "proxy"}]})

    result = prepare_config_update(source + " " + source, router)

    assert result.updated_config["outbounds"] == [
        {"type": "vless", "tag": "proxy", "server": "vpn.test"}
    ]
    assert result.warnings == (
        "Конфиг повторялся несколько раз. Импортирована одна копия.",
    )


def test_router_json_remains_strict_when_documents_repeat():
    source = '{"outbounds": [{"type": "vless"}]}'
    router = '{"outbounds": [{"type": "direct", "tag": "proxy"}]}'

    with pytest.raises(ConfigParseError, match="current_router_config"):
        prepare_config_update(source, router + router)
    with pytest.raises(ConfigParseError, match="current_router_config"):
        summarize_router_outbound(router + router)


def test_prepare_config_update_wraps_invalid_router_json():
    with pytest.raises(
        ConfigParseError,
        match="current_router_config contains invalid JSON",
    ):
        prepare_config_update(
            json.dumps({"outbounds": [{"type": "vless", "server": "vpn.test"}]}),
            "{not-json",
        )


def test_prepare_config_update_rejects_oversized_json_before_parsing():
    with pytest.raises(ConfigParseError, match="safe limit"):
        prepare_config_update(" " * (MAX_CONFIG_BYTES + 1), "{}")


def test_prepare_config_update_wraps_excessive_json_nesting():
    nested = "[" * 2000 + "0" + "]" * 2000

    with pytest.raises(ConfigParseError, match="nested"):
        prepare_config_update(nested, "{}")


def test_prepare_config_update_rejects_unsupported_imported_config():
    with pytest.raises(ConfigPatchError):
        prepare_config_update(
            json.dumps({"outbounds": [{"protocol": "trojan"}]}),
            json.dumps({"outbounds": [{"type": "direct"}]}),
        )


def test_prepare_config_update_uses_first_supported_exported_proxy():
    exported_config_text = json.dumps(
        {
            "outbounds": [
                {"type": "direct", "tag": "source-direct"},
                {"type": "vless", "tag": "source-proxy", "server": "vpn.test"},
            ]
        }
    )
    router_config_text = json.dumps(
        {"outbounds": [{"type": "direct", "tag": "router"}]}
    )

    result = prepare_config_update(exported_config_text, router_config_text)
    updated_config = result.updated_config

    assert updated_config["outbounds"][0] == {
        "type": "vless",
        "tag": "router",
        "server": "vpn.test",
    }


@pytest.mark.parametrize(
    "router_config",
    [
        {},
        {"outbounds": [{"type": "direct"}]},
    ],
)
def test_prepare_config_update_rejects_router_without_valid_active_outbound(
    router_config,
):
    with pytest.raises(ConfigPatchError):
        prepare_config_update(
            json.dumps({"outbounds": [{"type": "vless", "server": "vpn.test"}]}),
            json.dumps(router_config),
        )


def test_summaries_mask_sensitive_values():
    result = prepare_config_update(
        json.dumps(
            {
                "outbounds": [
                    {
                        "type": "vless",
                        "uuid": "11111111-1111-1111-1111-111111111111",
                        "password": "new-password",
                        "tls": {"private_key": "new-private-key", "short_id": "abcd"},
                    }
                ]
            }
        ),
        json.dumps(
            {
                "outbounds": [
                    {
                        "type": "trojan",
                        "tag": "proxy",
                        "password": "old-password",
                        "tls": {"private_key": "old-private-key"},
                    }
                ]
            }
        ),
    )

    summaries_text = json.dumps(result.preview, ensure_ascii=False)

    assert "11111111-1111-1111-1111-111111111111" not in summaries_text
    assert "new-password" not in summaries_text
    assert "old-password" not in summaries_text
    assert "new-private-key" not in summaries_text
    assert "old-private-key" not in summaries_text
    new_summary = result.preview["new_outbound"]
    assert new_summary["uuid"] == "***"
    assert new_summary["password"] == "***"
    assert new_summary["tls"]["private_key"] == "***"
    assert new_summary["tls"]["short_id"] == "***"
