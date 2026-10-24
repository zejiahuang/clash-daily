"""探测 speedtest.net 测速服务器能否从 CI runner 访问。

为什么必须单独探针、不能靠猜：
- 这些服务器的 8080 端口**在部分网络下不可达**。本机（国内）实测：中国电信 4 个
  服务器全部超时，而香港 STC、日本 SoftEther 能通（延迟 176 ms、下载 1 MB 正常）。
- 但 CI runner 在美西，路径完全不同，结论可能相反。本机结果不能外推。
- 端点本身是**可用**的（已实测）：`/latency.txt` 返回 200 + 10 字节，
  `/download?size=N` 返回 200 + 正好 N 字节。所以失败只可能是路径问题，不是端点不存在。

⚠️ 每一批都带一个「已知会成功」的对照组（Cloudflare / gstatic）：
   全部失败时，先看对照组是不是也挂了 —— 否则会把「探针自己坏了」当成「方案不可行」。
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com/repos/spiritLHLS/speedtest.net-CN-ID/contents/"
UA = "Mozilla/5.0 (compatible; speedtest-probe/1.0)"
LATENCY_TIMEOUT = 6.0
DOWNLOAD_BYTES = 1_000_000
DOWNLOAD_TIMEOUT = 12.0

# 每个 CSV 取几个样本；顺序即优先级（三网优先，其次境外）
FILES = (
    ("CN_Telecom.csv", 3, "中国电信"),
    ("CN_Unicom.csv", 3, "中国联通"),
    ("CN_Mobile.csv", 3, "中国移动"),
    ("HK.csv", 2, "中国香港"),
    ("JP.csv", 2, "日本"),
    ("SG.csv", 2, "新加坡"),
)


def _headers() -> dict[str, str]:
    out = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        out["Authorization"] = f"Bearer {token}"
    return out


FIELDS = ("id", "country_code", "country", "city", "ip", "host", "port", "supplier")


def fetch_csv(name: str) -> list[dict]:
    req = urllib.request.Request(API + name, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    import base64

    text = base64.b64decode(payload["content"]).decode("utf-8", "replace")
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return []
    # ⚠️ CN_Telecom / CN_Unicom / CN_Mobile 这三个文件**没有表头**，第一行就是数据。
    # 用 DictReader 会把第一条服务器当成表头吃掉（实测：显示「共 0 个服务器」）。
    # 判据：第一行第一个字段能不能当整数 —— id 是数字，表头是 "id"。
    first = next(csv.reader([lines[0]]))
    has_header = not (first and first[0].strip().isdigit())
    if has_header:
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        rows = [dict(zip(FIELDS, r)) for r in csv.reader(io.StringIO(text)) if len(r) >= 8]
    return [r for r in rows if r.get("ip") and r.get("port")]


def probe(url: str, timeout: float, want: int = 0) -> tuple[str, float, int]:
    """返回 (状态, 耗时秒, 字节数)。状态是 'OK' 或异常类名。"""
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(want) if want else resp.read(64)
        return "OK", time.time() - started, len(data)
    except urllib.error.HTTPError as exc:
        return f"HTTP{exc.code}", time.time() - started, 0
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, time.time() - started, 0


def main() -> int:
    print("=" * 78)
    print("speedtest.net 测速服务器可达性探测")
    print("=" * 78)

    # ---- 对照组：这两条一定通，否则是探针自己坏了 --------------------------
    print("\n[对照组 · 已知会成功]")
    controls = (
        ("cloudflare speed 1MB", "https://speed.cloudflare.com/__down?bytes=1000000",
         DOWNLOAD_TIMEOUT, DOWNLOAD_BYTES),
        ("gstatic 204", "http://www.gstatic.com/generate_204", LATENCY_TIMEOUT, 0),
        ("github raw", "https://raw.githubusercontent.com/spiritLHLS/"
                       "speedtest.net-CN-ID/main/README.md", LATENCY_TIMEOUT, 0),
    )
    control_ok = 0
    for label, url, timeout, want in controls:
        status, cost, size = probe(url, timeout, want)
        ok = status == "OK"
        control_ok += 1 if ok else 0
        speed = f"{size / cost / 1048576:.1f} MB/s" if ok and want else ""
        print(f"  {'OK  ' if ok else 'FAIL'} {label:<22} {status:<18} "
              f"{cost * 1000:>6.0f} ms {size:>8} B {speed}")

    if control_ok == 0:
        print("\n⚠️ 对照组全部失败 ⇒ 是这台机器的出网有问题，"
              "下面的结果不能用来判断方案可行性。")
        return 2

    # ---- 被测服务器 --------------------------------------------------------
    reachable: list[dict] = []
    for filename, limit, label in FILES:
        try:
            rows = fetch_csv(filename)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{label}] CSV 拉取失败：{type(exc).__name__}: {exc}")
            continue
        print(f"\n[{label}] {filename} 共 {len(rows)} 个服务器，取前 {limit} 个")
        for row in rows[:limit]:
            ip, port = row["ip"], row["port"]
            city = (row.get("city") or "")[:12]
            base = f"http://{ip}:{port}"
            lat_status, lat_cost, lat_size = probe(base + "/latency.txt", LATENCY_TIMEOUT)
            dl_status, dl_cost, dl_size = probe(
                base + f"/download?size={DOWNLOAD_BYTES}", DOWNLOAD_TIMEOUT, DOWNLOAD_BYTES
            )
            speed = dl_size / dl_cost / 1048576 if dl_status == "OK" else 0.0
            mark = "OK  " if lat_status == "OK" else "FAIL"
            print(f"  {mark} {ip}:{port:<6} {city:<13} "
                  f"lat={lat_status}({lat_cost * 1000:.0f}ms) "
                  f"dl={dl_status}({speed:.1f} MB/s)")
            if lat_status == "OK" or dl_status == "OK":
                reachable.append({
                    "label": label, "ip": ip, "port": port, "city": city,
                    "supplier": row.get("supplier") or "",
                    "latency_ms": round(lat_cost * 1000),
                    "speed_mbps": round(speed, 2),
                    "latency_ok": lat_status == "OK",
                    "download_ok": dl_status == "OK",
                })

    print("\n" + "=" * 78)
    print(f"可达 {len(reachable)} 个")
    for item in reachable:
        print(f"  {item['label']:<8} {item['ip']}:{item['port']:<6} "
              f"lat={item['latency_ms']}ms dl={item['speed_mbps']} MB/s "
              f"{item['supplier'][:34]}")
    if not reachable:
        print("  （一个都不通）")

    with open("speedtest-probe.json", "w", encoding="utf-8") as fh:
        json.dump({"reachable": reachable, "controls_ok": control_ok},
                  fh, ensure_ascii=False, indent=2)
    print("\n已写出 speedtest-probe.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
