"""Run converted, anonymized HAPP exports through a real sing-box binary."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from singroute.core.patcher import select_exported_outbound

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("fixture_name", "trojan_mode"),
    (
        ("source_happ_trojan_tls.json", "tcp-tls"),
        ("source_happ_trojan_tls.json", "grpc-tls"),
        ("source_happ_trojan_tls.json", "tcp-reality"),
        ("source_happ_vless_reality_grpc.json", None),
    ),
)
def test_converted_happ_outbound_passes_real_singbox_check(
    tmp_path, fixture_name, trojan_mode
):
    binary = os.environ.get("SING_BOX_BINARY") or shutil.which("sing-box")
    if not binary:
        pytest.skip("sing-box binary is not available")

    source = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    if trojan_mode == "grpc-tls":
        stream_settings = source["outbounds"][0]["streamSettings"]
        stream_settings["network"] = "grpc"
        stream_settings["grpcSettings"] = {"serviceName": "trojan-test"}
        del stream_settings["tcpSettings"]
    elif trojan_mode == "tcp-reality":
        stream_settings = source["outbounds"][0]["streamSettings"]
        stream_settings["security"] = "reality"
        stream_settings["realitySettings"] = {
            "serverName": "trojan.example.test",
            "publicKey": "CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "shortId": "0123456789abcdef",
            "fingerprint": "firefox",
        }
        del stream_settings["tlsSettings"]
    outbound = select_exported_outbound(source)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"outbounds": [outbound]}), encoding="utf-8")

    checked = subprocess.run(
        [binary, "check", "-c", str(config_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert checked.returncode == 0, checked.stderr
