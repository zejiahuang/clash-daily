"""源站发现 + 订阅下载 + 解析。

实测到的源站结构共五种，各用一个适配器：

  kind="manifest"      页面里直接给订阅链接
                       —— manifest.dpdns.org/today/

  kind="index"         索引页 -> 当日文章（链接里带 YYYY-MM-DD）-> /uploads/*.yaml|txt
                       —— clash-node.com / v2rayfree.org / stelliberty.com / lancex.net
                       这四家是同一套 CMS 模板，共用一个适配器。

  kind="wp-dated"      WordPress：索引页 -> /free-nodes/YYYY-MM-DD/ -> nodesub.<域名>/YYYY/MM/*.yaml|txt
                       —— nodetoolbox.net / clashsuburl.com

  kind="github-raw"    仓库里固定文件名的 raw 地址（extra 里是文件名）
                       —— zhuhaiuk/free-nodes；TG-NAV/nodeshare（自身产出为空，见下）

  kind="gh-uploads"    仓库里按日期堆积的 uploads/YYYY/MM/<n>-YYYYMMDD.yaml|txt，取最新日期
                       —— freeclashverge/freeclashverge.github.io

实测记录（2026-10-24）：
  * lancex.net 的文章可抓，但订阅文件全在 node.lancex.net 上，该域整站 526
    （Cloudflare「源站证书无效」，根路径也一样），带不带 Referer、http/https 都 526。
    适配器保留：这是源站自己的故障，修好后自动恢复；失败时只记一条 warn，不影响其它源。
  * TG-NAV/nodeshare 的 clash.yaml / v2ray.txt 都是 0 字节。它自己的 run.sh 靠
    api-suc.0z.gs 这个 subconverter 生成，而该服务已失效 —— 所以它的产出会长期为空。
    因此给它配了 fallback：直接取它 run.sh 里声明的上游仓库（其中 free-nodes/clashfree 已 404，
    另两个可用）。fallback 只在本源产出为空时才用，日志会写明。
  * nodetoolbox 与 clashsuburl 的订阅正文字节数完全相同（6516B yaml / 4052B txt），
    解析出的 15 个节点也完全一致 —— 两家其实是同一份数据，去重会消掉，无需特殊处理。
  * freeclashverge 单日 uploads 里 2-YYYYMMDD.yaml 就有 1922 个节点；
    同一天的 .yaml 与 .txt 是同一批节点，所以先取 .yaml，为空才退回 .txt。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import yaml

from . import nodes as nodemod
from . import uri as urimod
from .web import FetchError, fetch, origin_of


@dataclass(frozen=True)
class Source:
    sid: str
    kind: str  # manifest | index | wp-dated | github-raw | gh-uploads
    url: str
    extra: tuple[str, ...] = ()      # github-raw: 文件名列表；manifest: 附加订阅地址
    fallback: tuple[str, ...] = ()   # 本源产出为空时才用的兜底地址


SOURCES: tuple[Source, ...] = (
    # ---- 1. 页面直接给订阅链接 ----
    Source("manifest", "manifest", "https://manifest.dpdns.org/today/",
           ("https://manifest.dpdns.org/free/latest/clash.yaml",)),

    # ---- 2~5. 同一套 CMS：索引页 -> 当日文章 -> /uploads/ ----
    Source("clash-node", "index", "https://clash-node.com/free-node/"),
    Source("v2rayfree", "index", "https://v2rayfree.org/"),
    Source("stelliberty", "index", "https://stelliberty.com/free-node/"),
    Source("lancex", "index", "https://lancex.net/free-node/"),

    # ---- 6~7. WordPress：/free-nodes/YYYY-MM-DD/ -> nodesub.<域名>/ ----
    Source("nodetoolbox", "wp-dated", "https://nodetoolbox.net/free-nodes/"),
    Source("clashsuburl", "wp-dated", "https://clashsuburl.com/free-nodes/"),

    # ---- 8. GitHub 固定文件名 ----
    Source("zhuhaiuk", "github-raw",
           "https://raw.githubusercontent.com/zhuhaiuk/free-nodes/main/",
           ("clash_config.yaml", "nodes.txt",
            "extra_clash_config.yaml", "extra_nodes.txt")),

    # ---- 9. TG-NAV：自身产出为空（subconverter 已失效），回退到它 run.sh 里的上游 ----
    Source("tg-nav", "github-raw",
           "https://raw.githubusercontent.com/TG-NAV/nodeshare/main/",
           ("clash.yaml", "v2ray.txt"),
           ("https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub",
            "https://raw.githubusercontent.com/ermaozi01/free_clash_vpn/main/"
            "subscribe/clash.yml")),

    # ---- 10. 按日期堆积的 uploads/ ----
    Source("freeclashverge", "gh-uploads", "freeclashverge/freeclashverge.github.io"),
)

_HREF_RE = re.compile(r"""href=["']([^"'#?]+)["']""", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_UPLOAD_RE = re.compile(
    r"""https?://[A-Za-z0-9.\-]+/uploads/[^\s"'<>\\)]+?\.(?:ya?ml|txt)""",
    re.IGNORECASE,
)
_ANY_FILE_RE = re.compile(
    r"""https?://[A-Za-z0-9.\-]+/[^\s"'<>\\)]+?\.(?:ya?ml|txt)""",
    re.IGNORECASE,
)
_WP_POST_RE = re.compile(r"free-nodes/(\d{4}-\d{2}-\d{2})/?", re.IGNORECASE)
_UPLOAD_STAMP_RE = re.compile(r"""uploads/\d{4}/\d{1,2}/(\d+)-(\d{8})\.(ya?ml|txt)$""",
                              re.IGNORECASE)


@dataclass
class SourceReport:
    sid: str
    urls: list[str] = field(default_factory=list)
    articles: list[str] = field(default_factory=list)
    proxies: list[dict] = field(default_factory=list)
    error: str | None = None
    skipped: int = 0
    note: str | None = None


def _looks_yaml(text: str) -> bool:
    # 注意：Clash 配置里 `proxies:` 往往在几千字节之后（前面是 port/dns 等），
    # 所以不能只看开头一小段——这是踩过的坑。
    return bool(re.search(r"^proxies\s*:", text, re.M))


def parse_subscription(text: str, source: str = "") -> list[dict]:
    """把订阅正文解析成 proxy 列表。兼容 Clash YAML / 明文 URI / base64 包体。"""
    body = (text or "").strip()
    if not body:
        return []

    if _looks_yaml(body):
        found = _from_yaml(body, source)
        if found:
            return found

    uris = urimod.extract_uris(body)
    if uris:
        return _from_uris(uris, source)

    decoded = urimod.b64decode_loose(body)
    if decoded:
        try:
            inner = decoded.decode("utf-8")
        except UnicodeDecodeError:
            return []
        if _looks_yaml(inner):
            found = _from_yaml(inner, source)
            if found:
                return found
        return _from_uris(urimod.extract_uris(inner), source)

    return []


def _from_yaml(text: str, source: str) -> list[dict]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    raw = doc.get("proxies")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for index, item in enumerate(raw):
        proxy = nodemod.normalize(item, fallback_name=f"{source}-{index}")
        if proxy:
            out.append(proxy)
    return out


def _from_uris(uris: list[str], source: str) -> list[dict]:
    out: list[dict] = []
    for index, link in enumerate(uris):
        proxy = urimod.parse_uri(link)
        if not proxy:
            continue
        proxy = nodemod.normalize(proxy, fallback_name=f"{source}-{index}")
        if proxy:
            out.append(proxy)
    return out


def _article_date(href: str) -> date | None:
    match = _DATE_RE.search(href)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _scan_article(html: str) -> list[str]:
    """从文章正文里找订阅地址：先找 /uploads/，再找任意 yaml/txt，最后找直接贴的分享链接。"""
    hits = [m.group(0) for m in _UPLOAD_RE.finditer(html)]
    if not hits:
        hits = [m.group(0) for m in _ANY_FILE_RE.finditer(html)]
    if not hits:
        hits = urimod.extract_uris(html)
    return list(dict.fromkeys(hits))


def _article_links(html: str, base: str) -> list[tuple[date, str]]:
    origin = origin_of(base).rstrip("/")
    seen: set[str] = set()
    found: list[tuple[date, str]] = []
    for match in _HREF_RE.finditer(html):
        href = match.group(1).strip()
        if "free-node/" not in href:
            continue
        if not re.search(r"\.html?$", href, re.IGNORECASE):
            continue
        url = href if href.startswith("http") else origin + "/" + href.lstrip("/")
        if url in seen:
            continue
        seen.add(url)
        stamp = _article_date(href)
        if stamp:
            found.append((stamp, url))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _wp_posts(html: str, base: str) -> list[tuple[date, str]]:
    """WordPress 的 /free-nodes/YYYY-MM-DD/ 文章链接。"""
    origin = origin_of(base).rstrip("/")
    seen: set[str] = set()
    found: list[tuple[date, str]] = []
    for match in _HREF_RE.finditer(html):
        href = match.group(1).strip()
        hit = _WP_POST_RE.search(href)
        if not hit:
            continue
        try:
            stamp = datetime.strptime(hit.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        url = href if href.startswith("http") else origin + "/" + href.lstrip("/")
        if not url.endswith("/"):
            url += "/"
        if url in seen:
            continue
        seen.add(url)
        found.append((stamp, url))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _fetch_articles(candidates: list[tuple[date, str]], *, max_articles: int,
                    log=print) -> tuple[list[str], list[str]]:
    """按日期从新到旧抓文章，抓到订阅地址就停。"""
    urls: list[str] = []
    articles: list[str] = []
    for _, article_url in candidates[:max_articles]:
        try:
            html = fetch(article_url, referer=origin_of(article_url))
        except FetchError as exc:
            log(f"    [warn] 文章抓取失败 {article_url}: {exc}")
            continue
        articles.append(article_url)
        hits = _scan_article(html)
        for hit in hits:
            if hit not in urls:
                urls.append(hit)
        if urls:
            break
    return urls, articles


def _gh_headers() -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_uploads(repo: str, *, max_files: int = 8, log=print) -> list[str]:
    """仓库 uploads/ 下按 YYYYMMDD 堆积的订阅文件，取最新那一天。

    优先用 trees API（一次请求拿到准确答案，带 token 时限额 5000/h）；
    API 不可用时退化为「按日期拼地址，从今天往前试 5 天，只试 0- 号文件」。
    """
    try:
        tree = json.loads(fetch(
            f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1",
            headers=_gh_headers(), retries=2, timeout=45,
        ))
        entries = [
            (e["path"], _UPLOAD_STAMP_RE.search(e["path"]))
            for e in tree.get("tree", [])
        ]
        entries = [(p, m) for p, m in entries if m]
        if entries:
            latest = max(m.group(2) for _, m in entries)
            same_day = sorted(
                ((int(m.group(1)), m.group(3).lower(), p) for p, m in entries
                 if m.group(2) == latest)
            )
            # 同一天里 .yaml 优先（.txt 常常是同一批节点的另一种封装，甚至 0 字节）
            ordered = ([p for _, ext, p in same_day if ext == "yaml"]
                       + [p for _, ext, p in same_day if ext != "yaml"])
            log(f"    uploads 最新日期 {latest}，{len(ordered)} 个文件")
            return [f"https://raw.githubusercontent.com/{repo}/main/{p}"
                    for p in ordered[:max_files]]
        log("    [warn] trees API 里没找到 uploads/YYYYMMDD 形态的文件")
    except Exception as exc:  # noqa: BLE001 - 退化为按日期拼
        log(f"    [warn] trees API 不可用（{type(exc).__name__}），改为按日期拼地址")

    today = datetime.now().date()
    for back in range(6):
        day = today - timedelta(days=back)
        stem = day.strftime("%Y%m%d")
        for month in (f"{day.month:02d}", str(day.month)):
            url = (f"https://raw.githubusercontent.com/{repo}/main/uploads/"
                   f"{day.year}/{month}/0-{stem}.yaml")
            try:
                if fetch(url, retries=1, timeout=30).strip():
                    log(f"    按日期命中 {url}")
                    return [url]
            except FetchError:
                continue
    return []


def discover(source: Source, *, max_articles: int = 3, log=print) -> tuple[list[str], list[str]]:
    """返回 (订阅URL列表, 已访问的文章URL列表)。"""
    if source.kind == "github-raw":
        urls = [
            source.url.rstrip("/") + "/" + name
            for name in source.extra
        ]
        return urls, []

    if source.kind == "gh-uploads":
        return _gh_uploads(source.url, log=log), []

    page = fetch(source.url, referer=origin_of(source.url))
    articles: list[str] = []

    if source.kind == "manifest":
        urls: list[str] = []
        for pattern in (
            r"""https?://[^\s"'<>\\]+?\.(?:ya?ml|txt)""",
            r"""["'](/[^"']+?\.(?:ya?ml|txt))["']""",
        ):
            for match in re.finditer(pattern, page):
                hit = match.group(1) if match.groups() else match.group(0)
                if hit.startswith("http"):
                    urls.append(hit)
                else:
                    urls.append(origin_of(source.url).rstrip("/") + hit)
        urls.extend(source.extra)
        return list(dict.fromkeys(urls)), articles

    if source.kind == "wp-dated":
        candidates = _wp_posts(page, source.url)
        if not candidates:
            raise FetchError(f"{source.sid}: 索引页没找到 /free-nodes/YYYY-MM-DD/ 形态的文章")
        return _fetch_articles(candidates, max_articles=max_articles, log=log)

    # kind == "index"
    candidates = _article_links(page, source.url)
    if not candidates:
        raise FetchError(f"{source.sid}: 索引页没找到任何带日期的文章链接")
    return _fetch_articles(candidates, max_articles=max_articles, log=log)


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).netloc or "").lower()


def collect(
    *,
    only: set[str] | None = None,
    max_articles: int = 3,
    log=print,
) -> list[SourceReport]:
    reports: list[SourceReport] = []
    for source in SOURCES:
        if only and source.sid not in only:
            continue
        report = SourceReport(sid=source.sid)
        log(f"  -> {source.sid}: {source.url}")
        try:
            urls, articles = discover(source, max_articles=max_articles, log=log)
            report.urls = urls
            report.articles = articles

            # 同一主机一旦失败就不再试它剩下的地址。
            # 依据：lancex 的 8 个订阅地址全在 node.lancex.net 上，该域整站 526
            # （Cloudflare 源站证书无效），逐个重试只是把同一条错误打 8 遍、
            # 每遍还带 3 次重试 —— 白白多等几十秒，日志也被淹掉。
            dead_hosts: set[str] = set()
            failures: list[tuple[str, str]] = []

            for url in urls:
                host = _host_of(url)
                if host in dead_hosts:
                    report.skipped += 1
                    continue
                try:
                    if url.startswith(("vless://", "vmess://", "trojan://", "ss://")):
                        parsed = _from_uris([url], source.sid)
                    else:
                        text = fetch(url, referer=origin_of(url))
                        parsed = parse_subscription(text, source.sid)
                    report.proxies.extend(parsed)
                except FetchError as exc:
                    failures.append((url, str(exc)))
                    if host:
                        dead_hosts.add(host)

            if failures:
                for _, message in failures[:3]:
                    log(f"    [warn] 订阅下载失败 {message}")
                if len(failures) > 3:
                    log(f"    [warn] ……另有 {len(failures) - 3} 个地址失败")
            if report.skipped:
                log(f"    [warn] 同一主机的其余 {report.skipped} 个地址本次跳过"
                    f"（{', '.join(sorted(dead_hosts))} 已判为不可用）")

            # 本源产出为空时才用兜底地址（TG-NAV 自身产出长期为空，见模块 docstring）
            if not report.proxies and source.fallback:
                log(f"    [warn] {source.sid} 自身产出为空，改用兜底上游 {len(source.fallback)} 个")
                report.note = "自身产出为空，已改用兜底上游"
                for url in source.fallback:
                    try:
                        text = fetch(url, referer=origin_of(url))
                        parsed = parse_subscription(text, f"{source.sid}-fallback")
                        report.proxies.extend(parsed)
                        if url not in report.urls:
                            report.urls.append(url)
                    except FetchError as exc:
                        log(f"    [warn] 兜底地址失败 {url}: {exc}")

            # 「0 个节点」有两种情况，别让它们都显示成 OK：
            # 一种是真的没找到订阅地址，一种是地址找到了但一个都下不下来（lancex 目前就是后者）。
            if not report.proxies and not report.note:
                if report.urls:
                    where = ", ".join(sorted(dead_hosts)) or "未知主机"
                    report.note = (f"{len(report.urls)} 个订阅地址全部下载失败"
                                   f"（{where} 不可用）")
                else:
                    report.note = "没找到任何订阅地址"

            log(f"     订阅 {len(urls)} 个 -> 解析出 {len(report.proxies)} 个候选节点")
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其它源
            report.error = f"{type(exc).__name__}: {exc}"
            log(f"     [error] {report.error}")
        reports.append(report)
    return reports
