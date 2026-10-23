"""TCP 预筛：先花很小的代价把死地址砍掉，再交给 mihomo 做真实代理测速。

只测 (host, port) 去重后的端点，因为成百上千个节点往往共用同一批入口。
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORTS = (443, 8443, 2053, 2083, 2087, 2096)


def tcp_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for family, socktype, proto, _, addr in infos[:3]:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def probe_endpoints(
    endpoints: list[tuple[str, int]],
    *,
    timeout: float = 3.0,
    workers: int = 128,
    log=print,
) -> dict[tuple[str, int], bool]:
    unique = list(dict.fromkeys(endpoints))
    log(f"  TCP 预筛：{len(unique)} 个去重端点，并发 {workers}，超时 {timeout}s")
    result: dict[tuple[str, int], bool] = {}
    if not unique:
        return result
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(tcp_alive, host, port, timeout): (host, port)
            for host, port in unique
        }
        done = 0
        for future in futures:
            key = futures[future]
            try:
                result[key] = future.result()
            except Exception:  # noqa: BLE001
                result[key] = False
            done += 1
            if done % 500 == 0:
                alive = sum(1 for v in result.values() if v)
                log(f"    ... {done}/{len(unique)}，存活 {alive}")
    alive = sum(1 for value in result.values() if value)
    log(f"  存活端点 {alive}/{len(unique)}")
    return result
