"""Exercise imported VLESS WebSocket/TLS through real local sing-box processes."""

from __future__ import annotations

import http.client
import json
import os
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from singroute.application.operation import prepare_config_update

FIXTURES = Path(__file__).parent / "fixtures"
UUID = "00000000-0000-4000-8000-000000000001"
WS_PATH = "/api/v1/picks/sample/queue"


@pytest.mark.parametrize("source_format", ["link", "happ-json"])
@pytest.mark.parametrize("early_data", [False, True])
def test_imported_ws_tls_outbound_carries_real_http_traffic(
    tmp_path, source_format, early_data
):
    binary = os.environ.get("SING_BOX_BINARY") or shutil.which("sing-box")
    if not binary:
        pytest.skip("Set SING_BOX_BINARY to exercise the real VLESS WebSocket/TLS path")
    certificate_path, key_path = _create_certificate(tmp_path)
    ws_port, proxy_port = _unused_ports(2)
    observed: dict[str, str] = {}
    server_config = {
        "inbounds": [
            {
                "type": "vless",
                "listen": "127.0.0.1",
                "listen_port": ws_port,
                "users": [{"uuid": UUID}],
                "transport": {"type": "ws", "path": WS_PATH},
            }
        ],
        "outbounds": [{"type": "direct"}],
    }
    if early_data:
        server_config["inbounds"][0]["transport"].update(
            max_early_data=2560, early_data_header_name="Sec-WebSocket-Protocol"
        )
    with (
        _http_target() as target,
        _singbox_process(binary, server_config, tmp_path / "server.json", ws_port),
        _tls_frontend(ws_port, certificate_path, key_path, observed) as tls_port,
    ):
        if source_format == "link":
            source = (
                f"vless://{UUID}@127.0.0.1:{tls_port}?type=ws&security=tls"
                f"&sni=tls.example.test&fp=firefox&host=ws.example.test&path={WS_PATH}"
            )
            if early_data:
                source += "%3Fed%3D2560"
        else:
            source_json = json.loads(
                (FIXTURES / "source_happ_vless_ws_tls.json").read_text(encoding="utf-8")
            )
            source_json["outbounds"][0]["settings"].update(
                address="127.0.0.1", port=tls_port
            )
            if early_data:
                source_json["outbounds"][0]["streamSettings"]["wsSettings"]["path"] += (
                    "?ed=2560"
                )
            source = json.dumps(source_json)
        router = {
            "inbounds": [
                {"type": "mixed", "listen": "127.0.0.1", "listen_port": proxy_port}
            ],
            "outbounds": [{"type": "direct", "tag": "router-proxy"}],
        }
        config = prepare_config_update(source, json.dumps(router)).updated_config
        # Trust only the local test certificate; certificate/SNI verification stays on.
        config["outbounds"][0]["tls"]["certificate_path"] = str(certificate_path)
        with _singbox_process(binary, config, tmp_path / "client.json", proxy_port):
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            try:
                connection.request(
                    "GET",
                    f"http://127.0.0.1:{target.server_port}/probe",
                    headers={"Connection": "close"},
                )
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == b"singroute-ws-tls-ok"
            finally:
                connection.close()
    assert observed == {
        "sni": "tls.example.test",
        "host": "ws.example.test",
        "path": WS_PATH,
    }


def _unused_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


def _create_certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.example.test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("tls.example.test")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "certificate.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def _singbox_process(binary, config, path: Path, listen_port):
    path.write_text(json.dumps(config), encoding="utf-8")
    log_path = path.with_suffix(".log")
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [binary, "run", "-c", str(path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, log_path.read_text(encoding="utf-8")
                try:
                    with socket.create_connection(
                        ("127.0.0.1", listen_port), timeout=0.1
                    ):
                        break
                except OSError:
                    assert time.monotonic() < deadline, log_path.read_text(
                        encoding="utf-8"
                    )
                    time.sleep(0.02)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@contextmanager
def _http_target():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "19")
            self.end_headers()
            self.wfile.write(b"singroute-ws-tls-ok")

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


@contextmanager
def _tls_frontend(ws_port, certificate_path, key_path, observed):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    context.set_servername_callback(
        lambda _socket, name, _context: observed.update(sni=name)
    )

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with context.wrap_socket(self.request, server_side=True) as client:
                client.settimeout(10)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    request += chunk
                    if len(request) > 65536:
                        return
                head = request.split(b"\r\n\r\n", 1)[0].decode("ascii")
                lines = head.split("\r\n")
                observed["path"] = lines[0].split(" ")[1]
                headers = {
                    key.lower(): value.strip()
                    for key, value in (line.split(":", 1) for line in lines[1:])
                }
                observed["host"] = headers.get("host", "")
                if observed["path"] != WS_PATH or observed["host"] != "ws.example.test":
                    client.sendall(
                        b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                    )
                    return
                with socket.create_connection(
                    ("127.0.0.1", ws_port), timeout=10
                ) as upstream:
                    upstream.sendall(request)
                    # Relay WebSocket frames unchanged to a real sing-box VLESS inbound.
                    while True:
                        readable = (
                            [client]
                            if client.pending()
                            else select.select([client, upstream], [], [], 1)[0]
                        )
                        for source in readable:
                            try:
                                data = source.recv(65536)
                            except OSError:
                                return
                            if not data:
                                return
                            destination = upstream if source is client else client
                            try:
                                destination.sendall(data)
                            except OSError:
                                return

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=5)
