"""Strict sing-box connection conversion; native Xray connections stay native."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from singroute.core.errors import XrayConversionError


def _only(value: dict[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise XrayConversionError(
            "Параметры подключения не имеют безопасного соответствия в Xray."
        )


def to_xray(server: dict[str, Any]) -> dict[str, Any]:
    _only(
        server,
        {
            "type",
            "tag",
            "server",
            "server_port",
            "uuid",
            "password",
            "flow",
            "tls",
            "transport",
            "remarks",
            "remark",
            "name",
        },
    )
    protocol = server.get("type")
    if not isinstance(protocol, str) or protocol not in {
        "vless",
        "trojan",
        "hysteria2",
    }:
        raise XrayConversionError(
            "Этот протокол sing-box пока нельзя перенести в Xray."
        )
    if (protocol == "vless" and "password" in server) or (
        protocol != "vless" and ("uuid" in server or "flow" in server)
    ):
        raise XrayConversionError("Несовместимые параметры протокола сервера.")
    address, port = server.get("server"), server.get("server_port")
    if (
        not isinstance(address, str)
        or not address.strip()
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise XrayConversionError("Некорректный адрес или порт сервера.")
    stream: dict[str, Any] = {"network": "tcp", "security": "none"}
    tls = server.get("tls", {})
    if not isinstance(tls, dict):
        raise XrayConversionError("Некорректные настройки TLS.")
    _only(tls, {"enabled", "server_name", "insecure", "alpn", "utls", "reality"})
    if tls.get("enabled"):
        reality, utls = tls.get("reality", {}), tls.get("utls", {})
        if not isinstance(reality, dict) or not isinstance(utls, dict):
            raise XrayConversionError("Некорректные настройки TLS.")
        _only(reality, {"enabled", "public_key", "short_id"})
        _only(utls, {"enabled", "fingerprint"})
        security = "reality" if reality.get("enabled") else "tls"
        stream["security"] = security
        settings = {}
        for native, xray in (
            ("server_name", "serverName"),
            ("insecure", "allowInsecure"),
            ("alpn", "alpn"),
        ):
            if native in tls:
                settings[xray] = deepcopy(tls[native])
        if utls.get("enabled"):
            settings["fingerprint"] = utls.get("fingerprint", "chrome")
        if security == "reality":
            settings.update(
                publicKey=reality.get("public_key"), shortId=reality.get("short_id", "")
            )
        stream[f"{security}Settings"] = settings
    elif tls:
        _only(tls, {"enabled"})

    transport = server.get("transport", {})
    if not isinstance(transport, dict):
        raise XrayConversionError("Некорректный транспорт сервера.")
    network = transport.get("type", "tcp")
    if network == "grpc":
        _only(transport, {"type", "service_name"})
        stream.update(
            network="grpc",
            grpcSettings={"serviceName": transport.get("service_name", "")},
        )
    elif network == "ws":
        _only(
            transport,
            {"type", "path", "headers", "max_early_data", "early_data_header_name"},
        )
        path = transport.get("path", "/")
        early = transport.get("max_early_data", 0)
        if early:
            if (
                type(early) is not int
                or not 0 < early <= 8192
                or transport.get("early_data_header_name") != "Sec-WebSocket-Protocol"
                or not isinstance(path, str)
                or "ed=" in path
            ):
                raise XrayConversionError(
                    "Early data WebSocket нельзя безопасно перенести."
                )
            path += ("&" if "?" in path else "?") + f"ed={early}"
        stream.update(
            network="ws",
            wsSettings={
                "path": path,
                "headers": deepcopy(transport.get("headers", {})),
            },
        )
    elif transport:
        raise XrayConversionError(
            "Этот транспорт sing-box пока нельзя перенести в Xray."
        )

    settings = {"address": address, "port": port}
    if protocol == "vless":
        identity = server.get("uuid")
        if not isinstance(identity, str) or not identity:
            raise XrayConversionError("Не указан идентификатор VLESS.")
        user = {"id": identity, "encryption": "none"}
        flow = server.get("flow", "")
        if flow:
            if flow != "xtls-rprx-vision" or network != "tcp":
                raise XrayConversionError("Несовместимые параметры VLESS flow.")
            user["flow"] = flow
        settings = {"vnext": [{**settings, "users": [user]}]}
    else:
        password = server.get("password")
        if not isinstance(password, str) or not password:
            raise XrayConversionError("Не указан пароль сервера.")
        if protocol == "trojan":
            settings = {"servers": [{**settings, "password": password}]}
        else:
            if transport or stream["security"] != "tls":
                raise XrayConversionError(
                    "Hysteria2 требует TLS и транспорта Hysteria."
                )
            stream.update(
                network="hysteria", hysteriaSettings={"version": 2, "auth": password}
            )
            settings["version"] = 2
            protocol = "hysteria"
    result = {"protocol": protocol, "settings": settings, "streamSettings": stream}
    if "tag" in server:
        result["tag"] = server["tag"]
    return result
