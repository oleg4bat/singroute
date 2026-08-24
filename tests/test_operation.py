import json

import pytest

from singbox_outbound_updater.application.operation import (
    prepare_config_update,
    summarize_router_outbound,
)
from singbox_outbound_updater.core.errors import ConfigParseError, ConfigPatchError


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


def test_prepare_config_update_accepts_imported_and_router_config_strings():
    imported_config_content = json.dumps(
        {"outbounds": [{"type": "vless", "tag": "imported", "server": "vpn.test"}]}
    )
    current_router_config_content = json.dumps(
        {
            "dns": {"servers": ["1.1.1.1"]},
            "outbounds": [
                {"type": "hysteria2", "tag": "router", "server": "old.test"}
            ],
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
            {
                "outbounds": [
                    {"type": "vless", "tag": "router", "server": "old.test"}
                ]
            }
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
def test_prepare_config_update_wraps_invalid_imported_json():
    with pytest.raises(
        ConfigParseError,
        match="imported_config contains invalid JSON",
    ):
        prepare_config_update("{not-json", "{}")


def test_prepare_config_update_wraps_invalid_router_json():
    with pytest.raises(
        ConfigParseError,
        match="current_router_config contains invalid JSON",
    ):
        prepare_config_update(
            json.dumps({"outbounds": [{"type": "vless", "server": "vpn.test"}]}),
            "{not-json",
        )


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
    router_config_text = json.dumps({"outbounds": [{"type": "direct", "tag": "router"}]})

    result = prepare_config_update(exported_config_text, router_config_text)
    updated_config = result.updated_config

    assert updated_config["outbounds"][0] == {
        "type": "vless",
        "tag": "router",
        "server": "vpn.test",
    }
def test_prepare_config_update_rejects_non_list_router_outbounds():
    with pytest.raises(ConfigPatchError, match=r'router_config\["outbounds"\].*list'):
        prepare_config_update(
            json.dumps({"outbounds": [{"type": "vless", "server": "vpn.test"}]}),
            json.dumps({"outbounds": {"type": "direct"}}),
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
