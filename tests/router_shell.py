"""Execute production shell scripts with small stand-ins for OpenWrt tools."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from singroute.application.router_client import CommandResult


def run_router_shell(
    command,
    *,
    root=None,
    installed=(),
    active=(),
    settings=None,
    runtime_commands=None,
    boot=None,
    stopped_code=5,
    pending="",
    input_text=None,
):
    original_command = command
    shell = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
    if not Path(shell).is_file():
        pytest.skip("bash is required for router shell integration")
    settings = settings or {}
    boot = boot or {name: True for name in installed}
    runtime_commands = runtime_commands or {}
    q = shlex.quote
    script = []
    for name in ("sing-box", "xray"):
        alias = name.replace("-", "_")
        script.append(
            f"singroute_has_{alias}() {{ return {0 if name in installed else 1}; }}"
        )
        script.append(
            f'singroute_{alias}() {{ case "$1" in\n'
            f"status) printf '%s\\n' {q('running' if name in active else 'not running')}; return {0 if name in active else stopped_code};;\n"
            f"enabled) return {0 if boot.get(name) else 1};;\n"
            "*) return 127;; esac; }"
        )
        command = command.replace(
            f"test -x /etc/init.d/{name}", f"singroute_has_{alias}"
        )
        command = command.replace(f"/etc/init.d/{name}", f"singroute_{alias}")
    binaries = "\n".join(
        f"'-v:{name}') printf '%s\\n' /usr/bin/{name}; return 0;;" for name in installed
    )
    script.append(
        'command() { case "$1:$2" in\n'
        + binaries
        + "\n'-v:sing-box'|'-v:xray') return 1;;\n*) builtin command \"$@\";; esac; }"
    )
    cases = []
    for key, value in settings.items():
        cases.append(f"{q('get:' + key)}) printf '%s\\n' {q(value)};;")
    shown = "\n".join(
        key
        + "="
        + " ".join(
            q(part)
            for part in (value.split() if key.endswith(".conffiles") else [value])
        )
        for key, value in settings.items()
        if key.startswith("xray.")
    )
    cases.extend(
        [
            f"show:xray) printf '%s\\n' {q(shown)};;",
            f"changes:xray) printf '%s' {q(pending)};;",
        ]
    )
    script.append(
        'uci() { [ "$1" != -q ] || shift; case "$1:$2" in\n'
        + "\n".join(cases)
        + "\n*) return 1;; esac; }"
    )
    services = {
        name: {
            "instances": {"main": {"running": True, "command": runtime_commands[name]}}
            if name in active
            else {}
        }
        for name in installed
    }
    script.append(f"ubus() {{ printf '%s\\n' {q(json.dumps(services))}; }}")
    if root is not None:
        command = re.sub(
            r"/(?:etc|tmp|opt)/[^\s'\";)]+",
            lambda m: (root / m[0].lstrip("/")).as_posix(),
            command,
        )
    release = (
        (root / "etc/openwrt_release").as_posix()
        if root is not None
        else "/etc/openwrt_release"
    )
    script.append(
        f'cat() {{ if [ "$1" = {q(release)} ]; then printf \'%s\\n\' "DISTRIB_DESCRIPTION=\'OpenWrt local-test\'"; else command cat "$@"; fi; }}'
    )
    environment = {
        **os.environ,
        "PATH": str(Path(shell).parent.parent / "usr/bin")
        + os.pathsep
        + os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        [shell, "--noprofile", "--norc", "-c", "\n".join(script) + "\n" + command],
        input=input_text,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return CommandResult(
        original_command, result.returncode, result.stdout, result.stderr
    )


class ShellRouter:
    """Small router for discovery/SSH tests; files and shell execution are real."""

    def __init__(self, root, installed, missing=""):
        self.root, self.installed = root, set(installed)
        self.active = set(installed)
        self.calls = []
        self.runtime_paths = {name: f"/etc/{name}/config.json" for name in installed}
        self.uci_settings = {"sing-box.main.conffile": "/etc/sing-box/config.json"}
        if "xray" in installed:
            self.uci_settings.update(
                {
                    "xray.config.conffiles": "/etc/xray/config.json",
                    "xray.enabled.enabled": "1",
                    "xray.config.format": "json",
                }
            )
        for name in installed:
            server = (
                {
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": "router.test",
                                "port": 443,
                                "users": [{"id": "test-secret"}],
                            }
                        ]
                    },
                }
                if name == "xray"
                else {
                    "type": "vless",
                    "server": "router.test",
                    "server_port": 443,
                    "uuid": "test-secret",
                }
            )
            if name != missing:
                self.put(
                    self.runtime_paths[name],
                    json.dumps({"outbounds": [{**server, "tag": "proxy"}]}),
                )

    def path(self, remote):
        return self.root / remote.lstrip("/")

    def put(self, remote, content):
        path = self.path(remote)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def set_uci(self, values):
        self.uci_settings.update(values)

    def run(self, command, input_text=None):
        self.calls.append(command)
        runtime_commands = {
            name: [
                f"/usr/bin/{name}",
                "run",
                "-c" if name == "sing-box" else "-config",
                path,
            ]
            for name, path in self.runtime_paths.items()
        }
        return run_router_shell(
            command,
            root=self.root,
            installed=self.installed,
            active=self.active,
            settings=self.uci_settings,
            runtime_commands=runtime_commands,
            input_text=input_text,
        )
