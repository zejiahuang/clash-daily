"""节点规范化、合法性过滤、去重。"""

from __future__ import annotations

import ipaddress
import json
import re

# mihomo 能加载的类型白名单；不在其中的直接丢掉，避免污染配置
SUPPORTED_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
    "tuic", "http", "socks5", "anytls", "snell", "ssh", "wireguard", "vmess",
}

# 输出时的键顺序，其余键按字母序追加
KEY_ORDER = [
    "name", "type", "server", "port", "udp",
    "uuid", "alterId", "cipher", "password",
    "tls", "sni", "servername", "alpn", "flow",
    "client-fingerprint", "skip-cert-verify", "reality-opts",
    "network", "ws-opts", "grpc-opts", "h2-opts", "http-opts",
    "plugin", "plugin-opts", "obfs", "obfs-password",
    "protocol", "protocol-param", "obfs-param",
]

_BAD_HOSTS = {"localhost", "0.0.0.0", "::", "::1", ""}


def _is_private_host(host: str) -> bool:
    if host in _BAD_HOSTS:
        return True
    if host.lower().endswith(".local") or host.lower().endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified or ip.is_multicast


def _credential_of(proxy: dict) -> str:
    for key in ("uuid", "password"):
        if proxy.get(key):
            return f"{key}={proxy[key]}"
    return json.dumps(
        {k: proxy.get(k) for k in ("cipher", "plugin", "plugin-opts") if proxy.get(k)},
        sort_keys=True,
        ensure_ascii=False,
    )


def normalize(proxy: dict, *, fallback_name: str = "") -> dict | None:
    """校验 + 清洗 + 排序键。不合法返回 None。"""
    if not isinstance(proxy, dict):
        return None

    ptype = str(proxy.get("type") or "").strip().lower()
    if ptype not in SUPPORTED_TYPES:
        return None

    server = str(proxy.get("server") or "").strip()
    if not server or _is_private_host(server):
        return None

    try:
        port = int(str(proxy.get("port")).strip())
    except (TypeError, ValueError):
        return None
    if not (0 < port < 65536):
        return None

    cleaned: dict = {}
    for key, value in proxy.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        cleaned[key] = value
    cleaned["type"] = ptype
    cleaned["server"] = server
    cleaned["port"] = port

    name = str(cleaned.get("name") or "").strip()
    if not name:
        name = fallback_name or f"{server}:{port}"
    cleaned["name"] = name

    # vmess 必须有 uuid，ss/ssr 必须有 cipher+password，其余按类型粗校验
    if ptype in ("vmess", "vless") and not cleaned.get("uuid"):
        return None
    if ptype in ("ss", "ssr") and not (cleaned.get("cipher") and cleaned.get("password")):
        return None
    if ptype in ("trojan", "hysteria2", "anytls") and not cleaned.get("password"):
        return None
    if ptype == "tuic" and not (cleaned.get("uuid") or cleaned.get("token")):
        return None

    ordered: dict = {}
    for key in KEY_ORDER:
        if key in cleaned:
            ordered[key] = cleaned[key]
    for key in sorted(cleaned):
        if key not in ordered:
            ordered[key] = cleaned[key]
    return ordered


def dedupe_key(proxy: dict) -> tuple:
    return (
        proxy["type"],
        str(proxy["server"]).lower(),
        proxy["port"],
        _credential_of(proxy),
    )


def dedupe(proxies: list[dict]) -> tuple[list[dict], int]:
    seen: set = set()
    out: list[dict] = []
    for proxy in proxies:
        key = dedupe_key(proxy)
        if key in seen:
            continue
        seen.add(key)
        out.append(proxy)
    return out, len(proxies) - len(out)


def sanitize_label(text: str, limit: int = 40) -> str:
    """清掉控制字符和会破坏 YAML/JSON 的字符。"""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
