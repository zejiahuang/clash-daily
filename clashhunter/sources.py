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

  kind="direct"        订阅地址就是接口本身，没有文件扩展名，页面里也不用解析
                       —— zmxoo.xyz（tian.zmxoo.xyz/subapi）

  kind="dated-sub"     索引页 -> 纯数字文章 ID（/13783）-> 正文里的 sub/clash-YYYYMMDD.yaml
                       —— xinshoucun.net

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

新增源实测（2026-10-25，本机 + 境外抓取各一次）：
  * hello-world-1989/cn-news 的 clash-free.yaml 有 531 KB，但 `proxies:` 里只有
    **6 个节点**（vless×2 / ss×2 / hysteria2×2），其余是 proxy-groups。文件大不等于节点多。
  * sunmiao4458/free-proxy-airport 的 output/clash.yaml 是 23 个（vless 17 / vmess 2 /
    trojan 3 / ss 1），docs/clash.yaml 是同一份（同尺寸 9744 B），只取 output/ 一个。
  * zmxoo.xyz 的页面里订阅是 `https://tian.zmxoo.xyz/subapi`（Clash）与 `/linkapi`
    （小火箭 base64）。**路径没有扩展名**，`manifest` 的 `\\.(ya?ml|txt)` 正则看不见它，
    所以走 `direct`。正文里前三个是 `server: 0.0.0.0` 的公告条目，靠 nodes.py 的
    `_BAD_HOSTS` 过滤掉；真实节点 130 个。
  * xinshoucun.net 的文章 ID 是纯数字（`/13783`，没有日期），日期只在正文的
    `sub/clash-20261024.yaml` 里。索引页的链接顺序是 ID 倒序 ⇒ 取 ID 最大的那篇。
    单篇 168 个节点（ss 138 / trojan 20 / vmess 10）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote

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

    # ---- 11~12. GitHub 固定文件名（新增） ----
    # cn-news 的 clash-free.yaml 有 531 KB，但 proxies 里只有 6 个节点 —— 文件大是
    # 因为塞了整套 dns / rule-providers。别因为体积大就以为节点多。
    Source("cn-news", "github-raw",
           "https://raw.githubusercontent.com/hello-world-1989/cn-news/main/",
           ("clash-free.yaml",)),
    # 同一份文件在 docs/ 与 output/ 各有一份（9744 B，内容一致），只取一个：
    # 两份都取会让同一批节点各出现一次，虽然去重能消掉，但日志里会多一行假象。
    Source("sunmiao", "github-raw",
           "https://raw.githubusercontent.com/sunmiao4458/free-proxy-airport/main/",
           ("output/clash.yaml",)),

    # ---- 13. 订阅接口本身（无扩展名，见模块 docstring） ----
    Source("zmxoo", "direct", "https://zmxoo.xyz/",
           ("https://tian.zmxoo.xyz/subapi",)),

    # ---- 14. 纯数字文章 ID -> sub/clash-YYYYMMDD.yaml ----
    Source("xinshoucun", "dated-sub", "https://xinshoucun.net/category/freenode",
           ("https://xinshoucun.net/sub/clash-{date}.yaml",)),
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

# xinshoucun 的文章 ID 是纯数字且没有日期：https://xinshoucun.net/13783
# 必须整条路径就是一个数字才算 —— 否则 `wp-json/wp/v2/categories/76` 这种也会被当成文章。
_ARTICLE_ID_RE = re.compile(r"^https?://[^/]+/(\d{2,7})/?$")
# 正文里的订阅文件：https://xinshoucun.net/sub/clash-20261024.yaml
_DATED_SUB_RE = re.compile(
    r"""https?://[A-Za-z0-9.\-]+/sub/clash-\d{8}\.(?:ya?ml|txt)""", re.IGNORECASE
)


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
            # 文章抓取是「探测性」的：candidates 里还有别的日期，失败就换下一篇。
            # 所以这里**不重试、超时也压短** —— 默认的 3 次重试 × 30 s = 95 s，
            # 乘上 3 篇就是 4 分钟全用来等超时（实测 lancex 当天那篇握手超时，
            # 下一篇就正常，白等了 4 分钟）。
            html = fetch(article_url, referer=origin_of(article_url),
                         retries=1, timeout=20)
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


def _dated_subs(source: Source, *, max_articles: int = 3, log=print) -> list[str]:
    """索引页 -> 纯数字文章 ID -> 正文里的 `sub/clash-YYYYMMDD.yaml`。

    为什么要绕这一层而不是直接按日期拼地址：文章 ID 与日期无关（`/13783` 是
    2026-10-24 那篇），所以「今天是几号」推不出「哪篇文章是最新的」。文章里的
    文件名才是唯一可信的日期来源。

    兜底用 `{date}` 模板按日期拼（和 `_gh_uploads` 同一套思路）：文章页结构一变，
    这条路还能活。顺序是「先文章、后日期」，因为文章给的日期是**源站自己说的**。
    """
    page = fetch(source.url, referer=origin_of(source.url))
    origin = origin_of(source.url).rstrip("/")

    ids: dict[int, str] = {}
    for match in _HREF_RE.finditer(page):
        href = match.group(1).strip()
        url = href if href.startswith("http") else origin + "/" + href.lstrip("/")
        hit = _ARTICLE_ID_RE.match(url)
        if hit:
            ids[int(hit.group(1))] = url

    for _, article_url in sorted(ids.items(), reverse=True)[:max_articles]:
        try:
            html = fetch(article_url, referer=origin, retries=1, timeout=20)
        except FetchError as exc:
            log(f"    [warn] 文章抓取失败 {article_url}: {exc}")
            continue
        hits = [m.group(0) for m in _DATED_SUB_RE.finditer(html)]
        if hits:
            return list(dict.fromkeys(hits))

    if ids:
        log(f"    [warn] {len(ids)} 篇文章里都没找到 sub/clash-YYYYMMDD.yaml，改按日期拼")
    today = datetime.now().date()
    for back in range(6):
        url = source.extra[0].replace(
            "{date}", (today - timedelta(days=back)).strftime("%Y%m%d")
        )
        try:
            if fetch(url, referer=origin, retries=1, timeout=20).strip():
                log(f"    按日期命中 {url}")
                return [url]
        except FetchError:
            continue
    return []


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


# --------------------------------------------------------------------------- GitHub 自动搜索
# 静态清单只能覆盖「已经知道的那 14 个源站」，而 GitHub 上每天都有新的免费节点镜像仓。
# 这里在**采集阶段**顺带做一次仓库搜索，搜到的订阅和静态源走同一条流水线
# （去重 → TCP → 延迟 → 测速），没有特殊通道 —— 所以它们同样要证明自己能连上。
#
# 只查仓库（GET /search/repositories），不查代码：
#   * 仓库搜索未认证 10 次/分钟，默认 3 条查询 + 每仓 1 次 README = 11 次/轮；
#   * **代码搜索必须带 token**（未认证一律 401），所以不碰 `/search/code`。
# README 取不到或里面没有订阅地址时，按固定文件名试 raw —— raw.githubusercontent
# 不计 API 限额，404 也便宜。搜不到、限额用完、api.github.com 不可达都只记一条
# warn：这一源失败绝不能把整轮采集拖红。
#
# 想关掉：GITHUB_SEARCH=0。想提高限额：GITHUB_TOKEN / GH_TOKEN（见 _gh_headers）。
GH_SEARCH_REPOS = 8          # 每轮最多看几个仓（README 请求数 = 这个数）
GH_SEARCH_DAYS = 30          # 只看最近多少天有推送的仓 —— 停更的仓节点早失效了
GH_SEARCH_QUERIES = (
    "clash free nodes pushed:>{since}",
    "v2ray subscription nodes pushed:>{since}",
    "clash subscription free proxies pushed:>{since}",
)
# README 里宁可没写地址也不能空手而归时，按这些名字在 main 分支试一遍。
GH_COMMON_FILES = ("clash.yaml", "clash.yml", "sub.yaml", "config.yaml",
                   "nodes.yaml", "v2ray.txt", "nodes.txt", "sub.txt")

_REPO_IN_URL_RE = re.compile(
    r"(?:raw\.githubusercontent\.com|github\.com)/([^/\s]+/[^/\s]+?)(?:\.git)?(?:/|$)"
)
# README 里的 blob / raw 链接换成 raw 直链；否则抓回来的是 HTML 页面。
_GITHUB_BLOB_RE = re.compile(
    r"https?://github\.com/([^/\s]+/[^/\s]+)/(?:blob|raw)/([^/\s]+)/([^\s)\"'<>\]]+)"
)


def _known_repos() -> set[str]:
    """静态清单里已经接过的仓 —— 搜索结果里再出现就不重复抓一遍。"""
    known: set[str] = set()
    for source in SOURCES:
        # gh-uploads 的 url 就是裸的 owner/repo（没有主机名），单独收一下
        if source.kind == "gh-uploads":
            known.add(source.url.strip("/").lower())
        for url in (source.url, *source.extra, *source.fallback):
            match = _REPO_IN_URL_RE.search(url)
            if match:
                known.add(match.group(1).lower())
    return known


def _github_rawify(url: str) -> str:
    match = _GITHUB_BLOB_RE.match(url)
    if not match:
        return url
    return (f"https://raw.githubusercontent.com/{match.group(1)}/"
            f"{match.group(2)}/{match.group(3)}")


def _gh_search_repos(*, limit: int = GH_SEARCH_REPOS, log=print) -> list[str]:
    """仓库搜索：返回 `owner/repo` 列表，按更新时间倒序、已去重、不超过 limit。"""
    since = (datetime.now() - timedelta(days=GH_SEARCH_DAYS)).strftime("%Y-%m-%d")
    known = _known_repos()
    found: list[str] = []
    for template in GH_SEARCH_QUERIES:
        url = ("https://api.github.com/search/repositories?q="
               + quote(template.format(since=since), safe="")
               + f"&sort=updated&order=desc&per_page={max(3, limit)}")
        try:
            payload = json.loads(fetch(url, headers=_gh_headers(), retries=1, timeout=30))
        except Exception as exc:  # noqa: BLE001 - 单条查询失败不影响其它查询
            log(f"    [warn] GitHub 搜索失败（{type(exc).__name__}: {exc}）")
            continue
        for item in payload.get("items") or []:
            full = str(item.get("full_name") or "").strip()
            if not full or item.get("archived") or item.get("disabled") or item.get("fork"):
                continue
            if full.lower() in known or full in found:
                continue
            found.append(full)
            if len(found) >= limit:
                return found
    return found


def _gh_readme(repo: str) -> str:
    """取 README 正文。

    先走 raw.githubusercontent（**不计 API 限额**），大小写/文件名不标准时才退回
    API。实测未认证时 `api.github.com/repos/.../readme` 经常是 403 rate limit，
    而那正是最需要它工作的时候（一轮要读 8 个仓）。
    """
    for branch in ("main", "master"):
        for name in ("README.md", "readme.md"):
            try:
                text = fetch(f"https://raw.githubusercontent.com/{repo}/{branch}/{name}",
                             retries=1, timeout=20)
            except FetchError:
                continue
            if text.strip():
                return text
    headers = dict(_gh_headers())
    headers["Accept"] = "application/vnd.github.raw"
    return fetch(f"https://api.github.com/repos/{repo}/readme",
                 headers=headers, retries=1, timeout=25)


def _repo_subscription_urls(repo: str, readme: str, *, log=print) -> list[str]:
    """从 README 里抠订阅地址；一个都没有就按固定文件名试 raw。"""
    hits: list[str] = []
    for raw in _scan_article(readme):
        hit = _github_rawify(raw)
        # README 里的 docs / badge 也可能以 .yaml 结尾，先只挑像订阅的：
        # 分享链接，或者路径里带 clash / sub / node / proxy。
        scheme = hit.split("://", 1)[0].lower()
        if not (scheme in urimod.SCHEMES
                or re.search(r"(clash|sub|node|proxy|v2ray)", hit, re.IGNORECASE)):
            continue
        if hit not in hits:
            hits.append(hit)

    if not hits:
        base = f"https://raw.githubusercontent.com/{repo}/main/"
        for name in GH_COMMON_FILES:
            url = base + name
            try:
                if fetch(url, retries=1, timeout=15).strip():
                    hits.append(url)
                    break
            except FetchError:
                continue
    return hits[:3]


def _collect_github_search(*, log=print) -> SourceReport:
    """动态源：GitHub 仓库搜索 → README / 固定文件 → 订阅解析。

    返回的是一个普通 `SourceReport`，所以它在产物里和 14 个静态源长得一样，
    失败也只影响自己这一行。
    """
    report = SourceReport(sid="github-search")
    if os.environ.get("GITHUB_SEARCH", "1") == "0":
        report.note = "已按 GITHUB_SEARCH=0 关闭"
        return report

    try:
        repos = _gh_search_repos(log=log)
    except Exception as exc:  # noqa: BLE001
        report.error = f"{type(exc).__name__}: {exc}"
        log(f"     [error] {report.error}")
        return report

    if not repos:
        report.note = "GitHub 搜索没有命中（限额用完 / 网络不通 / 最近没有新仓），不影响其它源"
        log(f"    [warn] {report.note}")
        return report

    log(f"    命中 {len(repos)} 个仓库：{', '.join(repos)}")
    for repo in repos:
        try:
            readme = _gh_readme(repo)
        except Exception as exc:  # noqa: BLE001 - 单仓失败换下一个
            log(f"    [warn] {repo} 的 README 取不到（{type(exc).__name__}），"
                f"改按固定文件名试 raw")
            readme = ""

        urls = _repo_subscription_urls(repo, readme, log=log)
        got = 0
        for url in urls:
            if url not in report.urls:
                report.urls.append(url)
            try:
                # 分享链接本身就是节点，不能拿去 fetch（urllib 不认识 ss:// 这种 scheme）
                if url.split("://", 1)[0].lower() in urimod.SCHEMES:
                    parsed = _from_uris([url], f"gh-{repo.split('/')[-1]}")
                else:
                    text = fetch(url, referer=f"https://github.com/{repo}",
                                 retries=1, timeout=25)
                    parsed = parse_subscription(text, f"gh-{repo.split('/')[-1]}")
            except FetchError as exc:
                log(f"    [warn] {url} 下载失败：{exc}")
                continue
            if parsed:
                got += len(parsed)
                report.proxies.extend(parsed)
                report.articles.append(f"https://github.com/{repo}")
                break   # 一个仓只认第一份能解析的订阅，避免同一批节点重复计数
        log(f"    {repo}: {len(urls)} 个订阅地址 -> {got} 个节点")

    if not report.proxies:
        report.note = f"搜索到 {len(repos)} 个仓库，但没解析出节点"
    return report


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

    # `direct`：订阅地址就是接口本身。不去抓页面 —— 页面抓不到时（改版、Cloudflare
    # 挑战）连接口也一起没了，而接口才是唯一必要的东西。
    if source.kind == "direct":
        return list(source.extra), []

    if source.kind == "dated-sub":
        return _dated_subs(source, max_articles=max_articles, log=log), []

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

    # 动态源和静态源在**同一个步骤**里产出，后面的去重 / TCP / 延迟 / 测速完全一致。
    # 传了 --sources 时它默认不跑：手动排查单个源站时不该再额外打 GitHub。
    if not only or "github-search" in only:
        log("  -> github-search: 自动搜索近 30 天更新的订阅仓库")
        reports.append(_collect_github_search(log=log))

    return reports
