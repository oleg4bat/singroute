"""Local OpenWrt stand-in: real files/shell and real engine processes, no router."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from config_samples import UUID, xray_server
from local_proxy import _unused_ports
from router_shell import run_router_shell

from singroute.application.router_client import CommandResult


class LocalRouter:
    def __init__(self, root: Path, *, required_engines=("sing-box", "xray")):
        self.root = root
        self.binaries = {
            "sing-box": os.environ.get("SING_BOX_BINARY") or shutil.which("sing-box"),
            "xray": os.environ.get("XRAY_BINARY") or shutil.which("xray"),
        }
        self.shell = shutil.which("bash") or (
            "C:/Program Files/Git/bin/bash.exe"
            if Path("C:/Program Files/Git/bin/bash.exe").is_file()
            else None
        )
        if not all(self.binaries[name] for name in required_engines) or not self.shell:
            pytest.skip(
                "Set SING_BOX_BINARY and XRAY_BINARY; bash is required for real file replacement"
            )
        self.processes = {}
        self.logs = []
        self.boot = {"sing-box": True, "xray": False}
        self.calls = []
        self.fail_restart = None
        self.fail_check = None
        self.lose_install_response = False
        self.pending_uci = ""
        self.fail_uci_stage = False
        self.lose_uci_install_response = False
        self.transition_delays = {}
        self.transition_timers = []
        self.overlapping_engines = False
        self.path("/etc/config/xray").parent.mkdir(parents=True, exist_ok=True)
        self.path("/etc/config/xray").write_text(
            "config xray 'enabled'\n\toption enabled '1'\n"
            "config xray 'config'\n"
            "\toption conffiles '/etc/xray/config.json'\n"
            "\toption confdir ''\n\toption format 'json'\n"
            "\toption datadir '/usr/share/xray'\n"
            "\toption dialer 'preserved-dialer'\n",
            encoding="utf-8",
        )
        self.path("/tmp").mkdir(parents=True, exist_ok=True)
        self.put(
            "/etc/config/sing-box",
            "config sing-box 'main'\n\toption conffile '/etc/sing-box/config.json'\n",
        )

    @staticmethod
    def uci_values(text, package="xray"):
        values, section = {}, ""
        for line in text.splitlines():
            parts = shlex.split(line, comments=True)
            if len(parts) == 3 and parts[0] == "config":
                section = parts[2]
            elif len(parts) == 3 and parts[0] == "option":
                values[f"{package}.{section}.{parts[1]}"] = parts[2]
        return values

    def set_uci_option(self, path, key, value):
        section, option = key.split(".")[1:]
        lines, current, found = [], "", False
        for line in self.read_text(path).splitlines():
            parts = shlex.split(line, comments=True)
            if len(parts) == 3 and parts[0] == "config":
                current = parts[2]
            if (
                current == section
                and len(parts) == 3
                and parts[:2] == ["option", option]
            ):
                found = True
                if not value:
                    continue  # uci_set(...="") deletes the option.
                line = f"\toption {option} '{value}'"
            lines.append(line)
        assert found or not value, key
        self.put(path, "\n".join(lines) + "\n")

    @property
    def flag(self):
        return self.uci_values(self.read_text("/etc/config/xray"))[
            "xray.enabled.enabled"
        ]

    @flag.setter
    def flag(self, value):
        self.set_uci_option("/etc/config/xray", "xray.enabled.enabled", value)

    def path(self, remote: str) -> Path:
        result = self.root / remote.lstrip("/")
        assert result.resolve().is_relative_to(self.root.resolve())
        return result

    def read_text(self, path):
        return self.path(path).read_text(encoding="utf-8")

    def write_text(self, path, content):
        with self.path(path).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)

    def put(self, path, content):
        self.path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path(path).write_text(content, encoding="utf-8", newline="\n")

    def running(self, engine):
        process = self.processes.get(engine)
        return process is not None and process.poll() is None

    def stop(self, engine):
        process = self.processes.pop(engine, None)
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    def start(self, engine):
        self.stop(engine)
        config = (
            self.path(
                self.uci_values(self.read_text("/etc/config/sing-box"), "sing-box")[
                    "sing-box.main.conffile"
                ]
            )
            if engine == "sing-box"
            else self.path("/etc/xray/config.json")
        )
        log = (self.root / f"{engine}-{len(self.logs)}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        args = [
            self.binaries[engine],
            "run",
            "-c" if engine == "sing-box" else "-config",
            str(config),
        ]
        if engine == "xray":
            settings = self.uci_values(self.read_text("/etc/config/xray"))
            args = [self.binaries[engine], "run"]
            for file in settings["xray.config.conffiles"].split():
                args += ["-config", str(self.path(file))]
            if settings.get("xray.config.confdir"):
                args += ["-confdir", str(self.path(settings["xray.config.confdir"]))]
        self.processes[engine] = subprocess.Popen(
            args,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if self.running("sing-box") and self.running("xray"):
            self.overlapping_engines = True
        time.sleep(0.15)
        return self.running(engine)

    def close(self):
        for timer in self.transition_timers:
            timer.cancel()
            timer.join(timeout=15)
        for engine in list(self.processes):
            self.stop(engine)
        for log in self.logs:
            log.close()

    def run(self, command, input_text=None):
        self.calls.append(command)
        if command.startswith("# "):
            runtime = {}
            for name in self.binaries:
                if self.running(name):
                    args = []
                    for arg in self.processes[name].args[1:]:
                        try:
                            args.append(
                                "/" + Path(arg).relative_to(self.root).as_posix()
                            )
                        except ValueError:
                            args.append(arg)
                    runtime[name] = [f"/usr/bin/{name}", *args]
            settings = {}
            for name in self.binaries:
                settings.update(
                    self.uci_values(self.read_text("/etc/config/" + name), name)
                )
            return run_router_shell(
                command,
                root=self.root,
                installed={name for name, binary in self.binaries.items() if binary},
                active=set(runtime),
                settings=settings,
                runtime_commands=runtime,
                boot=self.boot,
                pending=self.pending_uci,
                input_text=input_text,
            )
        if command == "cat /etc/openwrt_release":
            return CommandResult(
                command, 0, "DISTRIB_DESCRIPTION='OpenWrt local-test'\n"
            )
        if command.startswith("command -v "):
            engine = command.split()[-1]
            installed = bool(self.binaries[engine])
            return CommandResult(
                command,
                0 if installed else 1,
                f"/usr/bin/{engine}\n" if installed else "",
            )
        if command == "test -r /etc/config/xray":
            return CommandResult(command, 0)
        if command.startswith("uci -q get "):
            key = command.split()[-1]
            package = key.split(".")[0]
            values = self.uci_values(self.read_text("/etc/config/" + package), package)
            return CommandResult(
                command, 0 if key in values else 1, values.get(key, "")
            )
        if command == "uci -q changes xray":
            return CommandResult(command, 0, self.pending_uci)
        if "uci -q -c " in command:
            if self.fail_uci_stage:
                return CommandResult(command, 1)
            # Only UCI is simulated; live compare/replace/rollback still runs
            # production shell commands against actual files below.
            for part in command.split(" && "):
                args = shlex.split(part)
                if args[0] == "mkdir":
                    self.path(args[1]).mkdir()
                elif "set" in args:
                    stage = args[args.index("-c") + 1]
                    key, value = args[-1].split("=", 1)
                    self.set_uci_option(stage + "/xray", key, value)
                else:
                    assert "commit" in args
            return CommandResult(command, 0)
        if command.startswith("uci set xray.enabled.enabled="):
            self.flag = command.split("=", 1)[1][0]
            return CommandResult(command, 0)
        if command.startswith("test -x /etc/init.d/"):
            return CommandResult(
                command, 0 if self.binaries[command.rsplit("/", 1)[-1]] else 1
            )
        if command.startswith("ubus call service list "):
            name = json.loads(shlex.split(command)[-1])["name"]
            instances = {}
            if self.running(name):
                args = self.processes[name].args
                remote_args = []
                for arg in args[1:]:
                    try:
                        remote_args.append(
                            "/" + Path(arg).relative_to(self.root).as_posix()
                        )
                    except ValueError:
                        remote_args.append(arg)
                instances["main"] = {
                    "running": True,
                    "command": [f"/usr/bin/{name}", *remote_args],
                }
            return CommandResult(
                command, 0, json.dumps({name: {"instances": instances}})
            )
        service = re.fullmatch(
            r"(?:sleep (\d+); )?/etc/init.d/(sing-box|xray) (\w+)", command
        )
        if service:
            pause, engine, action = service.groups()
            if pause:
                time.sleep(float(pause))
            if action == "status":
                return CommandResult(
                    command,
                    0 if self.running(engine) else 5,
                    "running\n" if self.running(engine) else "not running\n",
                )
            if action == "enabled":
                return CommandResult(command, 0 if self.boot[engine] else 1)
            if action in {"enable", "disable"}:
                self.boot[engine] = action == "enable"
                return CommandResult(command, 0)
            if action == "stop":
                delay = self.transition_delays.get((engine, action), 0)
                if delay:
                    timer = threading.Timer(delay, self.stop, args=(engine,))
                    timer.daemon = True
                    self.transition_timers.append(timer)
                    timer.start()
                    return CommandResult(command, 0)
                self.stop(engine)
                return CommandResult(command, 0)
            if action == "restart":
                if self.fail_restart == engine:
                    self.fail_restart = None
                    return CommandResult(command, 1)
                delay = self.transition_delays.get((engine, action), 0)
                if delay:
                    self.stop(engine)
                    timer = threading.Timer(delay, self.start, args=(engine,))
                    timer.daemon = True
                    self.transition_timers.append(timer)
                    timer.start()
                    return CommandResult(command, 0)
                return CommandResult(command, 0 if self.start(engine) else 1)
        actual_command = re.sub(r"^env XRAY_LOCATION_ASSET=\S+ ", "", command)
        if actual_command.startswith(("sing-box check -c ", "xray run -test ")):
            args = shlex.split(actual_command)
            engine = args[0]
            if self.fail_check == engine:
                return CommandResult(command, 1, stderr="synthetic validation failure")
            checked = subprocess.run(
                [self.binaries[engine], *args[1:-1], str(self.path(args[-1]))],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return CommandResult(
                command, checked.returncode, checked.stdout, checked.stderr
            )
        # Execute the production compare/write/backup/rollback shell commands.
        rewritten = re.sub(
            r"/(?:etc|tmp|opt)/[^\s'\";)]+",
            lambda m: self.path(m[0]).as_posix(),
            command,
        )
        shell_result = subprocess.run(
            [self.shell, "-c", rewritten],
            capture_output=True,
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if (
            self.lose_uci_install_response
            and "sha256sum" in command
            and "cp -p /etc/config/xray " in command
            and shell_result.returncode == 0
        ):
            self.lose_uci_install_response = False
            return CommandResult(command, -1)
        if (
            self.lose_install_response
            and "sha256sum" in command
            and shell_result.returncode == 0
        ):
            self.lose_install_response = False
            return CommandResult(command, -1)
        return CommandResult(
            command,
            shell_result.returncode,
            shell_result.stdout.decode("utf-8"),
            shell_result.stderr.decode("utf-8"),
        )


def wait_port(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("Local proxy did not start")


def setup_local(router):
    backend, proxy, other = _unused_ports(3)
    router.put(
        "/etc/sing-box/config.json",
        json.dumps(
            {
                "inbounds": [
                    {"type": "mixed", "listen": "127.0.0.1", "listen_port": other}
                ],
                "outbounds": [{"type": "direct", "tag": "proxy"}],
            }
        ),
    )
    router.put(
        "/etc/xray/config.json",
        json.dumps(
            {
                "inbounds": [
                    {"protocol": "http", "listen": "127.0.0.1", "port": proxy}
                ],
                "outbounds": [
                    xray_server("proxy"),
                    {"protocol": "freedom", "tag": "direct"},
                ],
            }
        ),
    )
    backend_config = {
        "log": {"loglevel": "debug"},
        "inbounds": [
            {
                "protocol": "vless",
                "listen": "127.0.0.1",
                "port": backend,
                "settings": {"clients": [{"id": UUID}], "decryption": "none"},
                "streamSettings": {"network": "tcp", "security": "none"},
            }
        ],
        "outbounds": [
            {
                "protocol": "freedom",
                "settings": {
                    "finalRules": [{"action": "allow", "ip": ["127.0.0.1/32"]}]
                },
            }
        ],
    }
    path = router.root / "backend.json"
    path.write_text(json.dumps(backend_config), encoding="utf-8")
    log = (router.root / "backend.log").open("w", encoding="utf-8")
    router.logs.append(log)
    process = subprocess.Popen(
        [router.binaries["xray"], "run", "-config", str(path)],
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    router.processes["backend"] = process
    wait_port(backend)
    source = {"outbounds": [xray_server("imported", port=backend)]}
    source["outbounds"][0]["settings"]["vnext"][0]["address"] = "127.0.0.1"
    return source, proxy, other
