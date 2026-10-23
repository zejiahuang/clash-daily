"""解析器自测：不联网，喂真实形态的分享链接，检查转出来的 Clash 字段。

运行：
    python -m tests.test_parsers        （或直接 python tests/test_parsers.py）
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter import nodes as nodemod  # noqa: E402
from clashhunter import uri as urimod  # noqa: E402


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


CASES: list[tuple[str, str, dict]] = [
    (
        "vmess ws+tls",
        "vmess://" + b64(json.dumps({
            "v": "2", "ps": "🇯🇵 JP-01", "add": "jp.example.com", "port": "443",
            "id": "11111111-2222-3333-4444-555555555555", "aid": "0", "scy": "auto",
            "net": "ws", "type": "none", "host": "cdn.example.com", "path": "/ray",
            "tls": "tls", "sni": "cdn.example.com",
        })),
        {"type": "vmess", "server": "jp.example.com", "port": 443, "network": "ws",
         "tls": True, "servername": "cdn.example.com",
         "ws-opts": {"path": "/ray", "headers": {"Host": "cdn.example.com"}}},
    ),
    (
        "vless reality",
        "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@1.2.3.4:9443"
        "?encryption=none&security=reality&sni=www.amazon.com&fp=chrome"
        "&pbk=PUBKEY123&sid=ab12&type=tcp&flow=xtls-rprx-vision#%E6%97%A5%E6%9C%AC-01",
        {"type": "vless", "server": "1.2.3.4", "port": 9443, "tls": True,
         "servername": "www.amazon.com", "flow": "xtls-rprx-vision",
         "reality-opts": {"public-key": "PUBKEY123", "short-id": "ab12"},
         "name": "日本-01"},
    ),
    (
        "vless ws+tls",
        "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@162.159.135.234:443"
        "?encryption=none&security=tls&sni=snip.edgeone.app&type=ws&host=snip.edgeone.app&path=%2F%3Fed%3D2048#us",
        {"type": "vless", "network": "ws", "tls": True,
         "ws-opts": {"path": "/?ed=2048", "headers": {"Host": "snip.edgeone.app"}}},
    ),
    (
        "trojan",
        "trojan://pass%40word@tr.example.com:8886?sni=tr.example.com&allowInsecure=1&type=ws&path=%2Fws#SG",
        {"type": "trojan", "server": "tr.example.com", "port": 8886,
         "password": "pass@word", "network": "ws", "ws-opts": {"path": "/ws"}},
    ),
    (
        "ss sip002",
        "ss://" + b64("aes-256-gcm:secretpw") + "@ss.example.com:8388#HK-01",
        {"type": "ss", "cipher": "aes-256-gcm", "password": "secretpw",
         "server": "ss.example.com", "port": 8388},
    ),
    (
        "ss 旧格式整体 base64",
        "ss://" + b64("chacha20-ietf-poly1305:pw123@ss2.example.com:443") + "#old",
        {"type": "ss", "cipher": "chacha20-ietf-poly1305", "password": "pw123",
         "port": 443},
    ),
    (
        "ss + simple-obfs",
        "ss://" + b64("aes-128-gcm:pw") + "@ss3.example.com:443"
        "?plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dbing.com#obfs",
        {"type": "ss", "plugin": "obfs",
         "plugin-opts": {"mode": "http", "host": "bing.com"}},
    ),
    (
        "hysteria2",
        "hysteria2://mypass@hy.example.com:8443?sni=hy.example.com&insecure=1"
        "&obfs=salamander&obfs-password=xyz#hy2",
        {"type": "hysteria2", "password": "mypass", "sni": "hy.example.com",
         "skip-cert-verify": True, "obfs": "salamander", "obfs-password": "xyz"},
    ),
    (
        "tuic",
        "tuic://uuid-here:pass-here@tu.example.com:443?congestion_control=bbr&alpn=h3#tuic",
        {"type": "tuic", "uuid": "uuid-here", "password": "pass-here",
         "congestion-controller": "bbr", "alpn": ["h3"]},
    ),
    (
        "ssr",
        "ssr://" + b64(
            "1.2.3.4:8080:auth_aes128_md5:aes-256-cfb:tls1.2_ticket_auth:"
            + b64("ssrpass") + "/?remarks=" + b64("SSR-节点")
        ),
        {"type": "ssr", "server": "1.2.3.4", "port": 8080,
         "protocol": "auth_aes128_md5", "obfs": "tls1.2_ticket_auth",
         "password": "ssrpass", "name": "SSR-节点"},
    ),
    (
        "vmess 老格式",
        "vmess://" + b64("auto:uuid-abc@1.2.3.4:443"),
        {"type": "vmess", "uuid": "uuid-abc", "cipher": "auto", "port": 443},
    ),
]


def check_subset(label: str, got: dict, want: dict) -> list[str]:
    problems = []
    for key, value in want.items():
        actual = got.get(key)
        if actual != value:
            problems.append(f"{label}: {key} 期望 {value!r}，实际 {actual!r}")
    return problems


def main() -> int:
    failures: list[str] = []
    for label, link, want in CASES:
        proxy = urimod.parse_uri(link)
        if proxy is None:
            failures.append(f"{label}: 解析返回 None")
            continue
        proxy = nodemod.normalize(proxy, fallback_name=label)
        if proxy is None:
            failures.append(f"{label}: normalize 丢弃了该节点")
            continue
        failures.extend(check_subset(label, proxy, want))

    # 边界：坏数据必须被安静丢弃，而不是抛异常
    for bad in ("vmess://not-base64!!!", "ss://", "vless://", "trojan://x@:0#a",
                "ss://" + b64("aes-256-gcm") + "@h:1#x"):
        try:
            urimod.parse_uri(bad)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"坏数据 {bad[:24]} 抛异常: {exc}")

    # 边界：私有地址/非法端口必须被 normalize 拦掉
    for item in (
        {"type": "ss", "server": "127.0.0.1", "port": 443, "cipher": "aes-128-gcm", "password": "x"},
        {"type": "ss", "server": "h", "port": 0, "cipher": "aes-128-gcm", "password": "x"},
        {"type": "ss", "server": "h", "port": 443, "cipher": "aes-128-gcm"},
        {"type": "nonsense", "server": "h", "port": 443},
    ):
        if nodemod.normalize(dict(item)) is not None:
            failures.append(f"normalize 未拦截非法节点: {item}")

    # 去重
    dupes, removed = nodemod.dedupe([
        {"type": "ss", "server": "a", "port": 1, "cipher": "c", "password": "p", "name": "1"},
        {"type": "ss", "server": "a", "port": 1, "cipher": "c", "password": "p", "name": "2"},
        {"type": "ss", "server": "a", "port": 2, "cipher": "c", "password": "p", "name": "3"},
    ])
    if len(dupes) != 2 or removed != 1:
        failures.append(f"dedupe 结果不对: {len(dupes)} 保留 / {removed} 去掉")

    total = len(CASES)
    if failures:
        print(f"✗ {len(failures)} 处失败（共 {total} 个用例）")
        for item in failures:
            print("   -", item)
        return 1
    print(f"✓ 全部通过：{total} 个协议用例 + 坏数据/非法节点/去重边界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
