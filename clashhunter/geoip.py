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
    """返回 {ip: (alpha2, 英文国名)}。失败返回已拿到的部分。

    ⚠️ 时间预算必须封顶。ip-api.com 是纯 HTTP，从国内 runner 上时通时不通
       （实测 2026-10-24：280 个 IP 分 3 批，整段花了 **3 分 24 秒**）。
       原来的参数是 25 s 超时 × 3 次 + 8/16 s 退避 —— 一批最坏 99 s，三批就是 5 分钟，
       而这只是给节点名加个国家。
       现在：12 s × 2 次 + 3 s 退避；**并且某一批彻底失败就跳过后面所有批**
       （同一个服务，前一批全挂后面不会突然好），把最坏情况压到 ~30 s。
    """
    unique = [ip for ip in dict.fromkeys(ips) if ip]
    out: dict[str, tuple[str, str]] = {}
    dead = False
    for start in range(0, len(unique), MAX_PER_REQUEST):
        chunk = unique[start:start + MAX_PER_REQUEST]
        batch_no = start // MAX_PER_REQUEST + 1
        if dead:
            log(f"    [warn] 第 {batch_no} 批跳过（前一批归属地查询全部失败）")
            continue
        got_before = len(out)
        for attempt in range(2):
            try:
                data = fetch_json(BATCH_URL, payload=chunk, timeout=12,
                                  referer="http://ip-api.com/")
                for item in data if isinstance(data, list) else []:
                    if item.get("status") == "success" and item.get("query"):
                        out[item["query"]] = (
                            (item.get("countryCode") or "").upper(),
                            item.get("country") or "",
                        )
                break
            except Exception as exc:  # noqa: BLE001
                log(f"    [warn] 归属地查询失败({batch_no}): "
                    f"{type(exc).__name__}: {exc}")
                if attempt < 1:
                    time.sleep(3)
        if len(out) == got_before:
            dead = True
            log("    [warn] 这一批一个都没查到 ⇒ 后面的批次不再尝试"
                "（国家判定退回「按服务端 IP 猜」）")
        if start + MAX_PER_REQUEST < len(unique):
            time.sleep(1.6)  # 别撞 45 次/分钟的限制
    log(f"  归属地成功 {len(out)}/{len(unique)}")
    return out
