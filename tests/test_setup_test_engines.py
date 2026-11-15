"""Run the engine setup script against real local HTTP, ZIPs and PowerShell."""

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows engine setup")
def test_setup_recovers_bad_cache_rejects_bad_download_and_reuses_verified_archives(
    tmp_path,
):
    source = Path("scripts/setup_test_engines.ps1").read_text(encoding="utf-8")
    entries = re.findall(
        r"Variable = '([^']+)'\s+Url = '([^']+)'\s+Sha256 = '([^']+)'\s+Executable = '([^']+)'",
        source,
    )
    assert entries
    payloads = {}
    for index, (variable, _, _, executable) in enumerate(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(executable, b"MZ" + variable.encode())
        payloads[f"/engine-{index}.zip"] = buffer.getvalue()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            data = b"broken-download" if len(requests) == 1 else payloads[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    script = tmp_path / "scripts/setup_test_engines.ps1"
    script.parent.mkdir()
    cache = tmp_path / ".pytest-engines"
    cache.mkdir()
    (cache / f"{entries[0][0]}.zip").write_bytes(b"broken-cache")
    exports = tmp_path / "github-env"
    environment = {**os.environ, "GITHUB_ENV": str(exports)}
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "PowerShell is required for Windows engine setup"
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        for index, (_, url, digest, _) in enumerate(entries):
            data = payloads[f"/engine-{index}.zip"]
            source = source.replace(
                url, f"http://127.0.0.1:{server.server_port}/engine-{index}.zip"
            )
            source = source.replace(digest, hashlib.sha256(data).hexdigest())
        script.write_text(source, encoding="utf-8-sig")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def run():
            return subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        try:
            failed = run()
            assert failed.returncode != 0
            assert "Unexpected archive checksum" in failed.stderr
            assert not list(cache.glob("*.part"))
            assert not exports.exists()
            assert not (cache / entries[0][3]).exists()

            recovered = run()
            assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            exported = dict(
                line.split("=", 1)
                for line in exports.read_text(encoding="utf-8-sig").splitlines()
            )
            for index, (variable, _, _, executable) in enumerate(entries):
                assert (cache / f"{variable}.zip").read_bytes() == payloads[
                    f"/engine-{index}.zip"
                ]
                assert Path(exported[variable]) == cache / executable
                assert (
                    Path(exported[variable]).read_bytes() == b"MZ" + variable.encode()
                )
            assert not list(cache.glob("*.part"))
            assert len(requests) == len(entries) + 1
            cached = run()
            assert cached.returncode == 0, cached.stdout + cached.stderr
            assert len(requests) == len(entries) + 1
        finally:
            server.shutdown()
            worker.join(timeout=5)
