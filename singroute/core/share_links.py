"""Parse single-server share links without network, filesystem or GUI access."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .converters import normalize_outbound_to_singbox
from .errors import SourceLinkError, UnsupportedVlessTransportError


def parse_share_link(text: str) -> dict[str, Any]:
    """Normalize one VLESS share link; never include source values in errors."""
    link = text.strip()
    if any(character.isspace() or ord(character) < 32 for character in link):
        raise SourceLinkError(
            "Вставьте одну ссылку сервера без пробелов и переносов строк."
        )
    try:
        parsed = urlsplit(link)
        if parsed.scheme != "vless":
            raise SourceLinkError("Поддерживаются только ссылки vless://.")
        server = parsed.hostname
        port = parsed.port
        if not server or port is None or not 1 <= port <= 65535:
            raise SourceLinkError(
                "В ссылке отсутствует или некорректен адрес или порт."
            )
        if parsed.password is not None or parsed.path or parsed.netloc.count("@") != 1:
            raise SourceLinkError("Некорректный формат ссылки VLESS.")
        uuid = str(UUID(_decode(parsed.username or "")))
    except ValueError as error:
        if isinstance(error, SourceLinkError):
            raise
        raise SourceLinkError(
            "Некорректный адрес, порт или UUID в ссылке VLESS."
        ) from None

    parameters: dict[str, str] = {}
    for part in parsed.query.split("&"):
        if not part:
            continue
        key, separator, value = part.partition("=")
        key, value = _decode(key), _decode(value)
        if not key or not separator or key in parameters:
            raise SourceLinkError(
                "Некорректные или повторяющиеся параметры ссылки VLESS."
            )
        parameters[key] = value
    # Decode even unused metadata to reject malformed escaping consistently.
    _decode(parsed.fragment)

    network = parameters.get("type", "tcp")
    if network not in {"tcp", "raw", "ws", "grpc"}:
        raise UnsupportedVlessTransportError(network)
    security = parameters.get("security", "none")
    if security not in {"none", "tls", "reality"}:
        raise SourceLinkError("Этот тип защиты VLESS пока не поддерживается.")
    allowed = {"type", "security", "encryption", "flow"}
    if network == "ws":
        allowed |= {"host", "path"}
    elif network == "grpc":
        allowed |= {"serviceName", "mode", "authority"}
    else:
        allowed.add("headerType")
        if parameters.get("headerType", "none") != "none":
            raise SourceLinkError("Маскировка TCP из этой ссылки не поддерживается.")
    if security != "none":
        allowed |= {"sni", "fp", "alpn"}
    if security == "tls":
        allowed |= {"allowInsecure", "insecure"}
    elif security == "reality":
        allowed |= {"pbk", "sid", "spx"}
    if any(key not in allowed and value for key, value in parameters.items()):
        raise SourceLinkError("Ссылка содержит неподдерживаемые параметры подключения.")

    stream: dict[str, Any] = {"network": network, "security": security}
    if security != "none":
        tls: dict[str, Any] = {
            "serverName": parameters.get("sni", server),
            "fingerprint": parameters.get("fp", "chrome"),
        }
        if not tls["serverName"] or not tls["fingerprint"]:
            raise SourceLinkError("В ссылке пустой SNI или TLS fingerprint.")
        if "alpn" in parameters:
            alpn = parameters["alpn"].split(",")
            if any(not item or item.strip() != item for item in alpn):
                raise SourceLinkError("Некорректный ALPN в ссылке VLESS.")
            tls["alpn"] = alpn
        if security == "tls":
            if "allowInsecure" in parameters and "insecure" in parameters:
                raise SourceLinkError("В ссылке повторяется настройка проверки TLS.")
            for key in ("allowInsecure", "insecure"):
                if key in parameters:
                    value = parameters[key]
                    if value not in {"0", "1", "false", "true"}:
                        raise SourceLinkError("Некорректная настройка проверки TLS.")
                    tls["allowInsecure"] = value in {"1", "true"}
            stream["tlsSettings"] = tls
        else:
            if not parameters.get("pbk"):
                raise SourceLinkError("В ссылке Reality отсутствует публичный ключ.")
            tls.update(
                publicKey=parameters["pbk"],
                shortId=parameters.get("sid", ""),
                spiderX=parameters.get("spx", ""),
            )
            stream["realitySettings"] = tls
    if network == "ws":
        stream["wsSettings"] = {"path": parameters.get("path", "/")}
        if parameters.get("host"):
            stream["wsSettings"]["headers"] = {"Host": parameters["host"]}
    elif network == "grpc":
        mode = parameters.get("mode", "gun")
        if mode not in {"gun", "multi"}:
            raise SourceLinkError("Этот режим gRPC пока не поддерживается.")
        stream["grpcSettings"] = {
            "serviceName": parameters.get("serviceName", ""),
            "multiMode": mode == "multi",
            "authority": parameters.get("authority", ""),
        }
    return normalize_outbound_to_singbox(
        {
            "protocol": "vless",
            "settings": {
                "address": server,
                "port": port,
                "id": uuid,
                "encryption": parameters.get("encryption", "none"),
                "flow": parameters.get("flow", ""),
            },
            "streamSettings": stream,
        }
    )


def _decode(value: str) -> str:
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise SourceLinkError("Некорректное кодирование ссылки VLESS.")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise SourceLinkError("Некорректное кодирование ссылки VLESS.") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise SourceLinkError("Недопустимые символы в ссылке VLESS.")
    return decoded
