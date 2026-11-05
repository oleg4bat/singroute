import json
from urllib.parse import quote, quote_plus

import pytest

from singroute.core.diagnostics import redact_config_output


def test_concrete_engine_error_remains_readable():
    output = (
        "failed to read config > unsupported config file extension: /tmp/config.new"
    )
    assert redact_config_output(output, {"password": "private-value"}) == output


@pytest.mark.parametrize(
    "encode",
    [lambda x: x, lambda x: json.dumps(x)[1:-1], quote, quote_plus],
)
def test_engine_echoes_of_old_and_new_credentials_are_masked(encode):
    old = 'old\\pass"phrase\nсекрет'
    new = "new password/+value"
    output = f"invalid password: {encode(old)}; rejected value: {encode(new)}"
    masked = redact_config_output(output, {"password": old}, {"password": new})
    assert masked == "invalid password: ***; rejected value: ***"


def test_nested_xray_credentials_headers_and_connection_paths_are_masked():
    config = {
        "settings": {"vnext": [{"users": [{"id": "client-uuid"}]}]},
        "streamSettings": {
            "realitySettings": {"publicKey": "reality-key", "shortIds": ["abcd"]},
            "wsSettings": {
                "headers": {"X-Custom": "header-secret"},
                "path": "/secret-path",
            },
        },
        "encryption": "mlkem-key",
        "auth": {"username": "private-user", "password": "private-password"},
    }
    values = [
        "client-uuid",
        "reality-key",
        "abcd",
        "header-secret",
        "/secret-path",
        "mlkem-key",
        "private-user",
        "private-password",
    ]
    assert redact_config_output(" | ".join(values), config) == " | ".join(
        ["***"] * len(values)
    )
    assert (
        redact_config_output("encryption: none", {"encryption": "none"})
        == "encryption: none"
    )


def test_overlapping_secrets_are_replaced_in_one_pass():
    assert (
        redact_config_output("abcdef abc ***", {"id": "abc", "password": "abcdef"})
        == "*** *** ***"
    )
