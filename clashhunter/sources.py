"""源站发现 + 订阅下载 + 解析。

四种源站实测结构：
  1. manifest.dpdns.org/today/  —— 页面里直接给 /free/latest/clash.yaml
  2. clash-node.com/free-node/  —— 索引页 -> 当日文章 -> node.clash-node.com/uploads/...
  3. v2rayfree.org/             —— 同上（node.v2rayfree.org）
  4. stelliberty.com/free-node/ —— 同上（node.stelliberty.com）

2/3/4 是同一套 CMS 模板，因此共用一个"索引 -> 最新文章 -> /uploads/ 链接"的适配器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from . import nodes as nodemod
from . import uri as urimod
from .web import FetchError, fetch, origin_of


@dataclass(frozen=True)
class Source:
    sid: str
    kind: str  # "index" | "manifest"
    url: str
    extra: tuple[str, ...] = ()


SOURCES: tuple[Source, ...] = (
    Source("manifest", "manifest", "https://manifest.dpdns.org/today/",
           ("https://manifest.dpdns.org/free/latest/clash.yaml",)),
    Source("clash-node", "index", "https://clash-node.com/free-node/"),
    Source("v2rayfree", "index", "https://v2rayfree.org/"),
    Source("stelliberty", "index", "https://stelliberty.com/free-node/"),
)

_HREF_RE = re.compile(r"""href=["']([^"'#?]+)["']""", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_UPLOAD_RE = re.compile(
    r"""https?://[A-Za-z0-9.\-]+/uploads/[^\s"'<>\\)]+?\.(?:ya?ml|txt)""",
    re.IGNORECASE,
)


@dataclass
class SourceReport:
    sid: str
    urls: list[str] = field(default_factory=list)
    articles: list[str] = field(default_factory=list)
    proxies: list[dict] = field(default_factory=list)
    error: str | None = None
    skipped: int = 0


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


def discover(source: Source, *, max_articles: int = 3, log=print) -> tuple[list[str], list[str]]:
    """返回 (订阅URL列表, 已访问的文章URL列表)。"""
    page = fetch(source.url, referer=origin_of(source.url))
    urls: list[str] = []
    articles: list[str] = []

    if source.kind == "manifest":
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

    candidates = _article_links(page, source.url)
    if not candidates:
        raise FetchError(f"{source.sid}: 索引页没找到任何带日期的文章链接")

    for _, article_url in candidates[:max_articles]:
        try:
            html = fetch(article_url, referer=origin_of(article_url))
        except FetchError as exc:
            log(f"    [warn] 文章抓取失败 {article_url}: {exc}")
            continue
        articles.append(article_url)
        hits = [m.group(0) for m in _UPLOAD_RE.finditer(html)]
        if not hits:
            # 站点换路径时的兜底：抓页面上任何 yaml/txt 链接
            hits = [
                m.group(0)
                for m in re.finditer(
                    r"""https?://[A-Za-z0-9.\-]+/[^\s"'<>\\)]+?\.(?:ya?ml|txt)""", html
                )
            ]
        if not hits:
            # 再兜底：文章里直接贴了分享链接
            hits = urimod.extract_uris(html)
        for hit in hits:
            if hit not in urls:
                urls.append(hit)
        if urls:
            break

    return urls, articles


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
            for url in urls:
                try:
                    if url.startswith(("vless://", "vmess://", "trojan://", "ss://")):
                        parsed = _from_uris([url], source.sid)
                    else:
                        text = fetch(url, referer=origin_of(url))
                        parsed = parse_subscription(text, source.sid)
                    report.proxies.extend(parsed)
                except FetchError as exc:
                    log(f"    [warn] 订阅下载失败 {url}: {exc}")
            log(f"     订阅 {len(urls)} 个 -> 解析出 {len(report.proxies)} 个候选节点")
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其它源
            report.error = f"{type(exc).__name__}: {exc}"
            log(f"     [error] {report.error}")
        reports.append(report)
    return reports
