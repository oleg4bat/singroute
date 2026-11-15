"""Anonymized configurations shared by unit and integration tests."""

import json
from pathlib import Path

UUID = "00000000-0000-4000-8000-000000000001"


def xray_server(tag, transport="tcp", port=443):
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "server.example.test",
                    "port": port,
                    "users": [{"id": UUID, "encryption": "none"}],
                }
            ]
        },
        "streamSettings": {"network": transport, "security": "none"},
    }


def xray_group(*servers):
    return {
        "outbounds": list(servers),
        "routing": {
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "main"}],
            "balancers": [
                {
                    "tag": "main",
                    "selector": ["proxy"],
                    "strategy": {"type": "leastLoad", "settings": {"expected": 7}},
                }
            ],
        },
        "dns": {"servers": ["source-dns.example.test"]},
    }


def router_config():
    return {
        "dns": {"servers": ["router-dns.example.test"]},
        "inbounds": [{"type": "tun", "interface_name": "tun0", "auto_route": False}],
        "route": {"final": "router-proxy", "rules": [{"outbound": "direct"}]},
        "outbounds": [
            {
                "type": "vless",
                "tag": "router-proxy",
                "server": "old.example.test",
                "server_port": 443,
                "uuid": UUID,
            },
            {"type": "direct", "tag": "direct"},
        ],
        "experimental": {"cache_file": {"enabled": True}},
    }


def native_group(count=12):
    servers = [xray_server(f"proxy-{index}") for index in range(count)]
    return {
        "log": {"access": "/client/log.txt"},
        "dns": {"servers": ["https://source.test/dns-query"]},
        "inbounds": [{"protocol": "socks", "port": 10808}],
        "outbounds": [
            *servers,
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
        "burstObservatory": {
            "subjectSelector": ["proxy"],
            "pingConfig": {
                "connectivity": "",
                "destination": "http://www.gstatic.com/generate_204",
                "interval": "2m",
                "sampling": 3,
                "timeout": "3s",
            },
        },
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "domainMatcher": "hybrid",
            "balancers": [
                {
                    "tag": "Auto_Balancer",
                    "selector": ["proxy"],
                    "fallbackTag": "proxy-0",
                    "strategy": {
                        "type": "leastLoad",
                        "settings": {
                            "baselines": ["200ms", "500ms"],
                            "costs": [{"match": "bypass", "regexp": True, "value": 3}],
                            "expected": 7,
                            "maxRTT": "1500ms",
                            "tolerance": 0,
                        },
                    },
                }
            ],
            "rules": [
                {"type": "field", "domain": ["domain:ru"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "Auto_Balancer"},
            ],
        },
    }


def xray_router():
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [
            {
                "tag": "tproxy-in",
                "protocol": "dokodemo-door",
                "listen": "0.0.0.0",
                "port": 1100,
                "settings": {"network": "tcp,udp", "followRedirect": True},
                "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
            }
        ],
        "outbounds": [],
        "routing": {"domainStrategy": "AsIs"},
    }


def tiered_group():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/source_xray_tiered_leastload.json"
        ).read_text(encoding="utf-8")
    )
