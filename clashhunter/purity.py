"""出口 IP 纯净度：IP 类型（机房/代理/移动）+ 欺诈分，合成一个 0~100 的数。

## 为什么不是「直接跑 IPQuality」

参考的是 <https://github.com/xykt/IPQuality> 的判定思路（基础信息 / IP 类型 /
风险评分三块），但**没有把这个脚本搬进来**，理由是量级对不上：

  * 它每个 IP 要打 8 个风险库、7 个流媒体、11 家邮局、400+ 个 DNSBL，
    单 IP 一分钟起步；这里每天要标 80 个节点，串起来就是一小时以上。
  * 其中 6 个库（IPQS / AbuseIPDB / IP2Location / ipregistry / ipapi / ipdata）
    **需要 API key**，而这个仓库一条密钥都不放（CNB 没有环境变量入口，
    写进公开仓库等于公开）。

所以这里只取两路**无密钥、单 IP 亚秒级**的信号，够回答「这个出口像不像
一个干净的住宅 IP」这个问题：

  1. **ip-api 的 `proxy` / `hosting` / `mobile`** —— 零额外请求，`geoip.lookup_batch`
     本来就要查这批 IP 的归属地，多要三个字段不多发一次请求（见 geoip.py 的注释）。
  2. **Scamalytics 的 Fraud Score（0~100）** —— IPQuality 的 8 个库里唯一免费
     且给单一总分的，抓 HTML 里的 `Fraud Score: N`。

## 分数的算法（纯函数，`score_of` 可离线复现）

    100 分起扣：被标为代理 -25、机房 IP -20、移动网络 -5；
    拿到欺诈分时再取 `min(分数, 100 - 欺诈分)` —— 第三方给出的总分优先，
    因为它是按实际滥用流量算的，比「是不是机房」这种静态属性更接近真相。

⚠️ 为什么 `mobile` 也扣分：移动网络大量是 CGNAT 共享出口，同一个 IP 后面可能有
   上万个用户，滥用率天然偏高。扣得少（5 分）只是因为它不如前两者确定。

⚠️ 分数缺失 ≠ 0 分。`proxy/hosting/mobile` 全 false 且没拿到欺诈分时，分数是 100，
   但那也可能是「三个标记恰好都干净」，也可能是「ip-api 没返回这个 IP」——
   这两种情况靠 `known` 字段区分，不靠分数。判据：`purity.known is False` 时
   分数一律不写进节点名（见 builder._finalize）。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from .web import UA, FetchError, fetch

# 三个标记各自的扣分。改这里必须同步 tests/test_purity.py 的期望值。
PENALTY_PROXY = 25
PENALTY_HOSTING = 20
PENALTY_MOBILE = 5

SCAM_URL = "https://scamalytics.com/ip/{ip}"
_FRAUD_RE = re.compile(r"Fraud Score:\s*(\d{1,3})")

# 欺诈分那一路的硬预算。80 个 IP × 6 并发 ≈ 30 s；卡在 90 s 是为了让「Scamalytics
# 被墙/被限速」这件事最多花 90 秒，而不是把一轮 20 分钟的流水线拖成 40 分钟。
FRAUD_BUDGET_S = 90.0
FRAUD_WORKERS = 6
FRAUD_TIMEOUT = 12


def kind_of(flags: dict) -> str:
    """IP 类型的单值标签。多个标记同时为真时按「越确定越优先」取一个。"""
    if flags.get("proxy"):
        return "代理"
    if flags.get("hosting"):
        return "机房"
    if flags.get("mobile"):
        return "移动"
    if flags.get("isp") or flags.get("org"):
        return "ISP"
    return "未知"


def score_of(flags: dict, fraud: int | None = None) -> int:
    """纯净度 0~100。纯函数 —— 缺陷在这里不会出声，所以 `tests/test_purity.py`
    把每一种标记组合都过了一遍。"""
    score = 100
    if flags.get("proxy"):
        score -= PENALTY_PROXY
    if flags.get("hosting"):
        score -= PENALTY_HOSTING
    if flags.get("mobile"):
        score -= PENALTY_MOBILE
    if fraud is not None:
        # 第三方总分是「这个 IP 有多脏」的直接度量，比静态属性更可信 ⇒ 取更严的那个。
        score = min(score, 100 - max(0, min(100, int(fraud))))
    return max(0, min(100, score))


def describe(flags: dict, *, fraud: int | None = None) -> dict:
    """给 report.json 用的完整描述。`known=False` 表示根本没拿到这个 IP 的资料。"""
    known = bool(flags)
    return {
        "known": known,
        "kind": kind_of(flags) if known else "未知",
        "score": score_of(flags, fraud) if known else None,
        "fraud": fraud,
        "proxy": bool(flags.get("proxy")),
        "hosting": bool(flags.get("hosting")),
        "mobile": bool(flags.get("mobile")),
        "isp": flags.get("isp") or "",
        "as": flags.get("as") or "",
    }


def fraud_scores(ips: list[str], *, budget: float = FRAUD_BUDGET_S,
                 workers: int = FRAUD_WORKERS, log=print) -> dict[str, int]:
    """Scamalytics 的欺诈分 {ip: 0~100}。抓不到就返回空 —— **绝不让它挡住出产物**。

    三个必须的约束，少一个就会变成新的故障点：

      * **总预算**（`budget`）：不是每请求超时，是整段墙钟。到点就停，
        已经拿到的照用。否则 80 个 × 12 s 的超时串起来是 16 分钟。
      * **单请求不重试**（`retries=1`）：重试在这里等于把预算翻倍，
        而「Scamalytics 打不开」通常是整站打不开，重试没有意义。
      * **解析不出就当没有**：`Fraud Score` 是页面上的文字，改版即失效。
        失效时返回空字典，分数退回只看 ip-api 的三个标记 —— 这是降级，不是错误。
    """
    unique = [ip for ip in dict.fromkeys(ips) if ip]
    if not unique:
        return {}

    started = time.monotonic()
    out: dict[str, int] = {}
    errors: list[str] = []

    def one(ip: str):
        if time.monotonic() - started > budget:
            return None
        try:
            html = fetch(SCAM_URL.format(ip=ip), retries=1,
                         timeout=FRAUD_TIMEOUT, referer="https://scamalytics.com/",
                         headers={"Accept": "text/html,application/xhtml+xml"})
        except FetchError as exc:
            errors.append(f"{ip}: {exc}")
            return None
        match = _FRAUD_RE.search(html)
        return (ip, int(match.group(1))) if match else None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for item in pool.map(one, unique):
            if item:
                out[item[0]] = item[1]

    spent = time.monotonic() - started
    if out:
        log(f"  欺诈分 {len(out)}/{len(unique)} 个（{spent:.0f}s，"
            f"中位 {sorted(out.values())[len(out) // 2]}）")
    else:
        # 一个都没拿到时，把第一条原因打出来 —— 否则「纯净度全是未知」
        # 和「Scamalytics 被墙」在日志里长得一样。
        why = errors[0] if errors else "页面上没有 Fraud Score 字段（可能改版）"
        log(f"  [warn] 欺诈分一个都没拿到（{spent:.0f}s），纯净度退回只看 IP 类型：{why}")
    return out


def annotate(records: list[dict], geo: dict[str, dict], *,
             fraud: dict[str, int] | None = None) -> dict[str, dict]:
    """把纯净度挂到每条记录上，返回 {节点名: 描述}。原地改 `records`。

    单独一个函数（而不是塞进 builder）是因为它也是纯数据变换：
    输入「记录 + 归属地 + 欺诈分」，输出「每条记录的纯净度」。
    """
    fraud = fraud or {}
    by_name: dict[str, dict] = {}
    for record in records:
        ip = record.get("exit_ip") or ""
        info = describe(geo.get(ip) or {}, fraud=fraud.get(ip))
        record["purity"] = info
        by_name[record["name"]] = info
    return by_name


def summary(by_name: dict[str, dict]) -> dict:
    """report 用的汇总：已知/未知各多少、类型分布、分数分布。"""
    known = [v for v in by_name.values() if v.get("known")]
    kinds: dict[str, int] = {}
    for info in known:
        kinds[info["kind"]] = kinds.get(info["kind"], 0) + 1
    scores = [info["score"] for info in known if info.get("score") is not None]
    return {
        "known": len(known),
        "unknown": len(by_name) - len(known),
        "with_fraud": sum(1 for v in known if v.get("fraud") is not None),
        "kinds": kinds,
        "median_score": sorted(scores)[len(scores) // 2] if scores else None,
    }
