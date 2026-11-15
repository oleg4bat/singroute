"""Real local processes and HTTP probes for proxy integration tests."""

import http.client
import json
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _unused_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


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
