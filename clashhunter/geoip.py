"""出口 IP 归属地：先解析域名，再用 ip-api 批量查询。

ip-api 免费额度是 45 次/分钟、每次最多 100 个 IP，所以整批只用几次请求。
查询失败不影响主流程，只是这些节点退回"按节点名猜国家"。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from .web import fetch_json

BATCH_URL = "http://ip-api.com/batch?fields=status,message,countryCode,country,query"
MAX_PER_REQUEST = 100


def resolve_one(host: str, timeout: float = 4.0) -> str | None:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
    if v4:
        return v4[0]
    v6 = [i[4][0] for i in infos if i[0] == socket.AF_INET6]
    return v6[0] if v6 else None


def resolve_many(hosts: list[str], *, workers: int = 48, log=print) -> dict[str, str]:
    unique = list(dict.fromkeys(hosts))
    log(f"  解析域名：{len(unique)} 个主机名")
    result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, ip in zip(unique, pool.map(resolve_one, unique)):
            if ip:
                result[host] = ip
    log(f"  解析成功 {len(result)}/{len(unique)}")
    return result


def lookup_batch(ips: list[str], *, log=print) -> dict[str, tuple[str, str]]:
    """返回 {ip: (alpha2, 英文国名)}。失败返回已拿到的部分。"""
    unique = [ip for ip in dict.fromkeys(ips) if ip]
    out: dict[str, tuple[str, str]] = {}
    for start in range(0, len(unique), MAX_PER_REQUEST):
        chunk = unique[start:start + MAX_PER_REQUEST]
        for attempt in range(3):
            try:
                data = fetch_json(BATCH_URL, payload=chunk, timeout=25,
                                  referer="http://ip-api.com/")
                for item in data if isinstance(data, list) else []:
                    if item.get("status") == "success" and item.get("query"):
                        out[item["query"]] = (
                            (item.get("countryCode") or "").upper(),
                            item.get("country") or "",
                        )
                break
            except Exception as exc:  # noqa: BLE001
                log(f"    [warn] 归属地查询失败({start // MAX_PER_REQUEST + 1}): "
                    f"{type(exc).__name__}: {exc}")
                if attempt < 2:
                    time.sleep(8 * (attempt + 1))
        else:
            log("    [warn] 该批归属地全部失败，跳过")
        if start + MAX_PER_REQUEST < len(unique):
            time.sleep(1.6)  # 别撞 45 次/分钟的限制
    log(f"  归属地成功 {len(out)}/{len(unique)}")
    return out
