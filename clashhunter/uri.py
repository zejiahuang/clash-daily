"""把分享链接（vmess:// vless:// trojan:// ss:// ssr:// hysteria2:// tuic://）
统一转成 Clash / mihomo 的 proxy 字典。

只做格式转换，不校验服务器是否可用——那是测速阶段的事。
任何一条解析失败都返回 None，由调用方丢弃并计数，绝不抛异常打断整批。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

SCHEMES = (
    "vless", "vmess", "trojan", "ss", "ssr",
    "hysteria2", "hy2", "hysteria", "tuic", "anytls",
)

# 在文本里找分享链接。结尾常见 </code>、"、)、, 等需要剥掉。
_URI_RE = re.compile(
    r"(?<![\w-])((?:%s)://[^\s\"'<>`\\]+)" % "|".join(SCHEMES),
    re.IGNORECASE,
)
_TRAILING = ").,;:、，。）》】」』\"'"


def b64decode_loose(text: str) -> bytes | None:
    if not text:
        return None
    s = re.sub(r"\s+", "", text)
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        # validate=True 很关键：否则任意中文正文都会被"成功"解码成垃圾字节，
        # 让"这段文本是不是 base64"这个判断失去意义。
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None


def _b64text(text: str) -> str | None:
    raw = b64decode_loose(text)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_uris(text: str) -> list[str]:
    out = []
    for match in _URI_RE.finditer(text or ""):
        uri = match.group(1).rstrip(_TRAILING)
        if len(uri) > 12:
            out.append(uri)
    return out


def _split_hostport(hostport: str) -> tuple[str, int] | None:
    hostport = hostport.strip()
    if hostport.startswith("["):  # [2001:db8::1]:443
        end = hostport.find("]")
        if end < 0:
            return None
        host = hostport[1:end]
        rest = hostport[end + 1:]
        if not rest.startswith(":"):
            return None
        port_s = rest[1:]
    else:
        if ":" not in hostport:
            return None
        host, port_s = hostport.rsplit(":", 1)
    try:
        port = int(port_s)
    except ValueError:
        return None
    host = host.strip().strip("[]")
    if not host or not (0 < port < 65536):
        return None
    return host, port


def _q(query: str) -> dict[str, str]:
    parsed = parse_qs(query or "", keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _transport(proxy: dict, net: str, host: str, path: str, service: str = "") -> None:
    """按 network 类型补 ws/grpc/h2/http 选项。"""
    net = (net or "tcp").lower()
    if net in ("ws", "httpupgrade"):
        proxy["network"] = "ws"
        opts: dict = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        if net == "httpupgrade":
            proxy["network"] = "ws"
            proxy["ws-opts"] = opts
            proxy["ws-opts"]["v2ray-http-upgrade"] = True
            return
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": service or path or ""}
    elif net in ("h2", "http"):
        proxy["network"] = "h2"
        h2: dict = {"path": path or "/"}
        if host:
            h2["host"] = [h.strip() for h in host.split(",") if h.strip()]
        proxy["h2-opts"] = h2
    elif net == "tcp" and path and path != "/":
        # tcp + http 伪装（vmess 老写法里用 type=http）
        pass


# --------------------------------------------------------------------------- vmess


def parse_vmess(uri: str) -> dict | None:
    body = uri[len("vmess://"):]
    payload = _b64text(body)
    if payload is None:
        # 少数客户端直接给明文
        payload = body
    payload = payload.strip()
    if payload.startswith("{"):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        server = str(obj.get("add") or "").strip()
        try:
            port = int(str(obj.get("port") or "").strip())
        except ValueError:
            return None
        uuid = str(obj.get("id") or "").strip()
        if not server or not uuid:
            return None
        net = str(obj.get("net") or "tcp")
        tls = str(obj.get("tls") or "").lower()
        host = str(obj.get("host") or "")
        path = str(obj.get("path") or "")
        proxy: dict = {
            "name": str(obj.get("ps") or "").strip() or f"{server}:{port}",
            "type": "vmess",
            "server": server,
            "port": port,
            "uuid": uuid,
            "alterId": int(str(obj.get("aid") or 0).strip() or 0),
            "cipher": str(obj.get("scy") or "auto"),
            "udp": True,
        }
        if tls in ("tls", "reality", "xtls"):
            proxy["tls"] = True
            proxy["skip-cert-verify"] = True
            sni = str(obj.get("sni") or host or "")
            if sni:
                proxy["servername"] = sni
        if obj.get("fp"):
            proxy["client-fingerprint"] = str(obj["fp"])
        alpn = obj.get("alpn")
        if alpn:
            proxy["alpn"] = [a for a in str(alpn).split(",") if a]
        _transport(proxy, net, host, path)
        if str(obj.get("type") or "").lower() == "http" and proxy.get("network") in (None, "tcp"):
            proxy["network"] = "http"
            hopt: dict = {"path": [path or "/"]}
            if host:
                hopt["headers"] = {"Host": [host]}
            proxy["http-opts"] = hopt
        return proxy

    # 老格式：auto:uuid@host:port
    match = re.match(r"^(?P<cipher>[\w-]+):(?P<uuid>[^@]+)@(?P<hp>.+)$", payload)
    if not match:
        return None
    hp = _split_hostport(match.group("hp"))
    if not hp:
        return None
    return {
        "name": f"{hp[0]}:{hp[1]}",
        "type": "vmess",
        "server": hp[0],
        "port": hp[1],
        "uuid": match.group("uuid"),
        "alterId": 0,
        "cipher": match.group("cipher"),
        "udp": True,
    }


# --------------------------------------------------------------------------- vless


def parse_vless(uri: str) -> dict | None:
    parts = urlsplit(uri)
    uuid = unquote(parts.username or "")
    host = parts.hostname
    port = parts.port
    if not uuid or not host or not port:
        return None
    q = _q(parts.query)
    name = unquote(parts.fragment or "").strip() or f"{host}:{port}"
    security = (q.get("security") or "none").lower()
    proxy: dict = {
        "name": name,
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "udp": True,
    }
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    if security in ("tls", "reality", "xtls"):
        proxy["tls"] = True
        sni = q.get("sni") or q.get("peer") or q.get("host") or ""
        if sni:
            proxy["servername"] = sni
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
        if security == "reality":
            proxy["reality-opts"] = {
                "public-key": q.get("pbk", ""),
                "short-id": q.get("sid", ""),
            }
            proxy["client-fingerprint"] = q.get("fp") or "chrome"
        elif _truthy(q.get("allowInsecure")) or _truthy(q.get("insecure")):
            proxy["skip-cert-verify"] = True
    if q.get("alpn"):
        proxy["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("packetEncoding"):
        proxy["packet-encoding"] = q["packetEncoding"]
    _transport(
        proxy,
        q.get("type", "tcp"),
        q.get("host", ""),
        q.get("path", ""),
        q.get("serviceName", "") or q.get("servicename", ""),
    )
    return proxy


# --------------------------------------------------------------------------- trojan


def parse_trojan(uri: str) -> dict | None:
    parts = urlsplit(uri)
    password = unquote(parts.username or "")
    host = parts.hostname
    port = parts.port
    if not password or not host or not port:
        return None
    q = _q(parts.query)
    proxy: dict = {
        "name": unquote(parts.fragment or "").strip() or f"{host}:{port}",
        "type": "trojan",
        "server": host,
        "port": port,
        "password": password,
        "udp": True,
        "skip-cert-verify": True,
    }
    sni = q.get("sni") or q.get("peer") or ""
    if sni:
        proxy["sni"] = sni
    if q.get("alpn"):
        proxy["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("fp"):
        proxy["client-fingerprint"] = q["fp"]
    if not _truthy(q.get("allowInsecure")) and not _truthy(q.get("insecure")):
        proxy.pop("skip-cert-verify", None)
    _transport(
        proxy,
        q.get("type", "tcp"),
        q.get("host", ""),
        q.get("path", ""),
        q.get("serviceName", "") or q.get("servicename", ""),
    )
    return proxy


# --------------------------------------------------------------------------- ss


def _ss_plugin(plugin: str) -> dict:
    if not plugin:
        return {}
    bits = [b for b in plugin.split(";") if b]
    if not bits:
        return {}
    kind = bits[0].strip().lower()
    opts: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" in bit:
            k, v = bit.split("=", 1)
            opts[k.strip().lower()] = v.strip()

    if kind in ("obfs-local", "simple-obfs", "obfs"):
        out: dict = {"plugin": "obfs", "plugin-opts": {"mode": opts.get("obfs", "http")}}
        if opts.get("obfs-host"):
            out["plugin-opts"]["host"] = opts["obfs-host"]
        return out
    if kind in ("v2ray-plugin", "v2ray"):
        po: dict = {"mode": "websocket"}
        if opts.get("host"):
            po["host"] = opts["host"]
        if opts.get("path"):
            po["path"] = opts["path"]
        po["tls"] = "tls" in opts or _truthy(opts.get("tls"))
        return {"plugin": "v2ray-plugin", "plugin-opts": po}
    if kind in ("shadow-tls", "shadowtls"):
        po = {"host": opts.get("host", ""), "password": opts.get("password", "")}
        if opts.get("version"):
            po["version"] = int(opts["version"])
        return {"plugin": "shadow-tls", "plugin-opts": po}
    return {}


def parse_ss(uri: str) -> dict | None:
    body = uri[len("ss://"):]
    name = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = unquote(frag).strip()
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    body = body.strip().rstrip("/")

    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        cred = _b64text(userinfo)
        if not cred or ":" not in cred:
            cred = unquote(userinfo)
        if ":" not in cred:
            return None
        method, password = cred.split(":", 1)
    else:
        decoded = _b64text(body)
        if not decoded or "@" not in decoded:
            return None
        cred, hostport = decoded.rsplit("@", 1)
        if ":" not in cred:
            return None
        method, password = cred.split(":", 1)

    hp = _split_hostport(hostport)
    if not hp:
        return None
    host, port = hp
    proxy: dict = {
        "name": name or f"{host}:{port}",
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method.strip(),
        "password": password,
        "udp": True,
    }
    proxy.update(_ss_plugin(_q(query).get("plugin", "")))
    return proxy


# --------------------------------------------------------------------------- ssr


def parse_ssr(uri: str) -> dict | None:
    decoded = _b64text(uri[len("ssr://"):])
    if not decoded:
        return None
    main, _, tail = decoded.partition("/?")
    fields = main.rsplit(":", 5)
    if len(fields) != 6:
        return None
    host, port_s, protocol, method, obfs, pw_b64 = fields
    try:
        port = int(port_s)
    except ValueError:
        return None
    pw = _b64text(pw_b64)
    if pw is None or not host:
        return None
    q = _q(tail)
    remarks = _b64text(q.get("remarks", "")) or f"{host}:{port}"
    obfs_param = _b64text(q.get("obfsparam", "")) or ""
    proto_param = _b64text(q.get("protoparam", "")) or ""
    return {
        "name": remarks.strip(),
        "type": "ssr",
        "server": host,
        "port": port,
        "cipher": method,
        "password": pw,
        "protocol": protocol,
        "obfs": obfs,
        "obfs-param": obfs_param,
        "protocol-param": proto_param,
        "udp": True,
    }


# --------------------------------------------------------------------------- hysteria2 / tuic / anytls


def parse_hysteria2(uri: str) -> dict | None:
    parts = urlsplit(uri)
    host = parts.hostname
    port = parts.port
    if not host or not port:
        return None
    auth = unquote(parts.username or "")
    if parts.password:
        auth = f"{auth}:{unquote(parts.password)}"
    q = _q(parts.query)
    proxy: dict = {
        "name": unquote(parts.fragment or "").strip() or f"{host}:{port}",
        "type": "hysteria2",
        "server": host,
        "port": port,
        "password": auth,
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if _truthy(q.get("insecure")) or _truthy(q.get("allowInsecure")):
        proxy["skip-cert-verify"] = True
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
        if q.get("obfs-password"):
            proxy["obfs-password"] = q["obfs-password"]
    if q.get("alpn"):
        proxy["alpn"] = [a for a in q["alpn"].split(",") if a]
    return proxy


def parse_tuic(uri: str) -> dict | None:
    parts = urlsplit(uri)
    host = parts.hostname
    port = parts.port
    if not host or not port:
        return None
    q = _q(parts.query)
    proxy: dict = {
        "name": unquote(parts.fragment or "").strip() or f"{host}:{port}",
        "type": "tuic",
        "server": host,
        "port": port,
        "uuid": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if _truthy(q.get("insecure")) or _truthy(q.get("allow_insecure")):
        proxy["skip-cert-verify"] = True
    if q.get("alpn"):
        proxy["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("congestion_control"):
        proxy["congestion-controller"] = q["congestion_control"]
    if q.get("udp_relay_mode"):
        proxy["udp-relay-mode"] = q["udp_relay_mode"]
    return proxy


def parse_anytls(uri: str) -> dict | None:
    parts = urlsplit(uri)
    host = parts.hostname
    port = parts.port
    if not host or not port:
        return None
    q = _q(parts.query)
    proxy: dict = {
        "name": unquote(parts.fragment or "").strip() or f"{host}:{port}",
        "type": "anytls",
        "server": host,
        "port": port,
        "password": unquote(parts.username or ""),
        "udp": True,
        "skip-cert-verify": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    return proxy


_PARSERS = {
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "ssr": parse_ssr,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "hysteria": parse_hysteria2,
    "tuic": parse_tuic,
    "anytls": parse_anytls,
}


def parse_uri(uri: str) -> dict | None:
    scheme = uri.split("://", 1)[0].lower()
    parser = _PARSERS.get(scheme)
    if parser is None:
        return None
    try:
        return parser(uri)
    except Exception:  # noqa: BLE001 - 单条坏数据不应影响整批
        return None
