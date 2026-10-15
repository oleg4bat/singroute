"""Exercise generated auto exits with real local sing-box processes."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_auto_outbounds import UUID, xray_group, xray_server
from test_vless_ws_integration import _singbox_process, _unused_ports

from singroute.application.operation import prepare_config_update


@contextmanager
def probe_target():
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "7")
            self.end_headers()
            self.wfile.write(b"auto-ok")

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join(timeout=5)


def proxy_request(proxy_port, target_port):
    connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=2)
    try:
        connection.request("GET", f"http://127.0.0.1:{target_port}/traffic")
        response = connection.getresponse()
        return response.status == 200 and response.read() == b"auto-ok"
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def test_generated_group_selects_working_node_and_carries_real_traffic(tmp_path):
    binary = os.environ.get("SING_BOX_BINARY") or shutil.which("sing-box")
    if not binary:
        pytest.skip("Set SING_BOX_BINARY for real automatic-exit validation")
    dead_port, server_port, proxy_port, controller_port = _unused_ports(4)
    source = xray_group(
        xray_server("proxy-dead", port=dead_port),
        xray_server("proxy-live", port=server_port),
    )
    source["outbounds"][0]["remarks"] = "🇩🇪 Недоступный сервер"
    source["outbounds"][1]["remarks"] = "🇪🇪 Локальный сервер"
    for server in source["outbounds"]:
        server["settings"]["vnext"][0]["address"] = "127.0.0.1"
    router = {
        "inbounds": [
            {"type": "mixed", "listen": "127.0.0.1", "listen_port": proxy_port}
        ],
        "outbounds": [{"type": "direct", "tag": "router-proxy"}],
        "experimental": {
            "clash_api": {"external_controller": f"127.0.0.1:{controller_port}"}
        },
    }
    config = prepare_config_update(
        json.dumps(source), json.dumps(router)
    ).updated_config
    assert config["outbounds"][0]["outbounds"] == [
        "🇩🇪 Недоступный сервер",
        "🇪🇪 Локальный сервер",
    ]
    assert config["outbounds"][0]["interval"] == "1m"
    assert config["outbounds"][0]["tolerance"] == 20
    assert config["outbounds"][0]["interrupt_exist_connections"] is True
    config_path = tmp_path / "generated.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    checked = subprocess.run(
        [binary, "check", "-c", str(config_path)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert checked.returncode == 0, checked.stderr
    backend = {
        "inbounds": [
            {
                "type": "vless",
                "listen": "127.0.0.1",
                "listen_port": server_port,
                "users": [{"uuid": UUID}],
            }
        ],
        "outbounds": [{"type": "direct"}],
    }
    with (
        probe_target() as target_port,
        _singbox_process(binary, backend, tmp_path / "server.json", server_port),
    ):
        # Keep all probes on loopback; only the test shortens the production interval.
        config["outbounds"][0]["url"] = f"http://127.0.0.1:{target_port}/probe"
        config["outbounds"][0]["interval"] = "1s"
        with _singbox_process(binary, config, tmp_path / "client.json", proxy_port):
            deadline = time.monotonic() + 20
            while not proxy_request(proxy_port, target_port):
                assert time.monotonic() < deadline, (
                    "URLTest did not select the working VLESS node"
                )
                time.sleep(0.1)
            connection = http.client.HTTPConnection(
                "127.0.0.1", controller_port, timeout=2
            )
            try:
                connection.request("GET", "/proxies/router-proxy")
                response = connection.getresponse()
                assert response.status == 200
                assert (
                    json.loads(response.read())["now"] == config["outbounds"][2]["tag"]
                )
            finally:
                connection.close()
    assert "outbound/vless[🇪🇪 Локальный сервер]" in (tmp_path / "client.log").read_text(
        encoding="utf-8"
    )
