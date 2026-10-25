"""源站适配器的离线回归测试（不联网）。

这些适配器全部是「正则 + 排序」的纯数据变换，缺陷在设备/CI 上不会出声：
表现为某个源站悄悄变成 0 个节点，而整条流水线照样跑完、照样出订阅。
所以把真实页面片段当夹具，逐条断言。

夹具全部来自 2026-10-24 的实测抓取结果，不是编的。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clashhunter import sources as source_mod  # noqa: E402
from clashhunter.sources import (  # noqa: E402
    SOURCES,
    _article_links,
    _dated_subs,
    _scan_article,
    _wp_posts,
    discover,
    parse_subscription,
)

# lancex.net 索引页片段（CMS 模板，文章是 .htm 结尾、日期在文件名里）
# 注意：实测索引页里每个文章链接会出现两次（列表区 + 侧栏），所以要断言去重生效。
LANCEX_INDEX = """
<li><a href="/free-node/clash-v2ray-ss-ssr-2026-10-24.htm">10月24日免费节点</a></li>
<li><a href="/free-node/clash-v2ray-ss-ssr-2026-10-24.htm">10月24日免费节点（侧栏重复）</a></li>
<li><a href="/free-node/2026-10-23-node-share-links.htm">10月23日免费节点</a></li>
<li><a href="/free-node/top-vpn-proxy-2026-10-22.htm">10月22日免费节点</a></li>
<li><a href="/news/article-166878.htm">无关文章</a></li>
"""

# nodetoolbox.net 索引页片段（WordPress，文章是 /free-nodes/YYYY-MM-DD/ 目录）
WP_INDEX = """
<a href="https://nodetoolbox.net/free-nodes/2026-10-24/">10月24日</a>
<a href="https://nodetoolbox.net/free-nodes/2026-10-23/">10月23日</a>
<a href="https://nodetoolbox.net/free-nodes/2026-10-22/">10月22日</a>
<a href="https://nodetoolbox.net/about/">关于</a>
"""

# nodetoolbox.net 文章正文片段：订阅地址在 nodesub 子域，不是 /uploads/
WP_ARTICLE = """
<p><strong>Clash订阅:</strong></p>
<code>https://nodesub.nodetoolbox.net/2026/10/nodetoolbox-20261024.yaml</code>
<p><strong>V2ray订阅:</strong></p>
<code>https://nodesub.nodetoolbox.net/2026/10/nodetoolbox-20261024.txt</code>
"""

# lancex.net 文章正文片段：订阅地址在 node.lancex.net/uploads/
LANCEX_ARTICLE = """
<code>https://node.lancex.net/uploads/2026/10/1-20261024.txt</code>
<code>https://node.lancex.net/uploads/2026/10/0-20261024.yaml</code>
<a href="https://lancex.net/paid-subscribe/">推荐机场</a>
"""

# 文章里只贴了分享链接（两种兜底路径都要能抓到）
INLINE_ARTICLE = """
<p>vless://11111111-2222-3333-4444-555555555555@example.com:443?type=ws#demo</p>
"""

# xinshoucun.net 分类页片段（2026-10-25 实测）：文章是纯数字 ID，**没有日期**，
# 而且同一篇会出现两次（标题链接 + 阅读全文）。导航栏里也有一堆数字页（/40、/137）。
XSC_INDEX = """
<a href="https://xinshoucun.net/13783">「新手村」10月24日 | 实测可用168个</a>
<a href="https://xinshoucun.net/13783">阅读全文</a>
<a href="https://xinshoucun.net/13780">「新手村」10月23日 | 实测可用xxx个</a>
<a href="https://xinshoucun.net/13777">「新手村」10月22日 | 实测可用xxx个</a>
<a href="https://xinshoucun.net/40">关于我们</a>
<a href="https://xinshoucun.net/wp-json/wp/v2/categories/76">分类 76</a>
"""

# 文章正文片段：订阅地址就在 <a href> 里，文件名带 YYYYMMDD —— 日期只在这里出现。
XSC_ARTICLE = """
<p style="margin:0 0 8px"><strong>Clash 订阅：</strong>
<a href="https://xinshoucun.net/sub/clash-20261024.yaml" target="_blank">https://xinshoucun.net/sub/clash-20261024.yaml</a></p>
<p>节点每天更新，建议每日重新导入获取最新节点。</p>
"""


def main() -> int:
    problems: list[str] = []

    # 1. 源站清单本身：sid 唯一、kind 都是已知值
    known_kinds = {"manifest", "index", "wp-dated", "github-raw", "gh-uploads",
                   "direct", "dated-sub"}
    sids = [s.sid for s in SOURCES]
    if len(sids) != len(set(sids)):
        problems.append(f"SOURCES 里有重复 sid: {sids}")
    # ⚠️ 这个数字是「接了几个源」，改 SOURCES 时必须同步 —— 少接一个就是少一批节点，
    #    而少接不会报错，只会安静地少几十个候选。
    if len(sids) != 14:
        problems.append(f"源站数量 {len(sids)} != 14（少接一个就是少一批节点）")
    for source in SOURCES:
        if source.kind not in known_kinds:
            problems.append(f"{source.sid} 的 kind={source.kind!r} 没有对应适配器")
        if source.kind in ("direct", "dated-sub", "github-raw") and not source.extra:
            problems.append(f"{source.sid} 的 kind={source.kind} 但没有 extra 地址")

    # 2. CMS 索引：.htm 也要认，且按日期倒序、去重
    arts = _article_links(LANCEX_INDEX, "https://lancex.net/free-node/")
    if len(arts) != 3:
        problems.append(f"lancex 索引应解析出 3 篇文章（含 .htm），实得 {len(arts)}: {arts}")
    if arts and arts[0][0] != date(2026, 10, 24):
        problems.append(f"lancex 最新文章日期 {arts[0][0]} != 2026-10-24")
    if [a[0] for a in arts] != sorted([a[0] for a in arts], reverse=True):
        problems.append("CMS 文章没有按日期倒序排列")
    if any("/news/" in url for _, url in arts):
        problems.append("CMS 索引把无关文章也算进来了")

    # 3. WordPress 索引：/free-nodes/YYYY-MM-DD/ 目录形态
    posts = _wp_posts(WP_INDEX, "https://nodetoolbox.net/free-nodes/")
    if len(posts) != 3:
        problems.append(f"WP 索引应解析出 3 篇，实得 {len(posts)}: {posts}")
    if posts and posts[0][0] != date(2026, 10, 24):
        problems.append(f"WP 最新文章日期 {posts[0][0]} != 2026-10-24")
    if posts and not posts[0][1].endswith("/"):
        problems.append(f"WP 文章 URL 应以 / 结尾，实得 {posts[0][1]!r}")
    if any("/about/" in url for _, url in posts):
        problems.append("WP 索引把非日期页面也算进来了")

    # 4. 文章扫描：三种形态都要抓到，且顺序是 uploads 优先
    lancex_hits = _scan_article(LANCEX_ARTICLE)
    if len(lancex_hits) != 2 or not all("/uploads/" in h for h in lancex_hits):
        problems.append(f"lancex 文章扫描结果不对: {lancex_hits}")
    wp_hits = _scan_article(WP_ARTICLE)
    if len(wp_hits) != 2 or not all("nodesub." in h for h in wp_hits):
        problems.append(f"nodesub 订阅地址没抓到（这类站不是 /uploads/ 路径）: {wp_hits}")
    inline_hits = _scan_article(INLINE_ARTICLE)
    if not inline_hits or not inline_hits[0].startswith("vless://"):
        problems.append(f"文章里直接贴分享链接时没兜底抓到: {inline_hits}")

    # 5. 两种真实订阅正文都要能解析
    yaml_body = (
        "port: 7890\nsocks-port: 7891\nallow-lan: true\nmode: Rule\n"
        "dns:\n  enable: true\n  nameserver:\n    - 223.5.5.5\n"
        "proxies:\n"
        "  - {name: a, type: ss, server: 1.2.3.4, port: 443, cipher: aes-128-gcm, password: x}\n"
    )
    got_yaml = parse_subscription(yaml_body, "t")
    if len(got_yaml) != 1:
        problems.append(f"完整 Clash 配置（proxies 在 dns 之后）解析出 {len(got_yaml)} 个，应为 1")

    b64_body = "dmxlc3M6Ly8xMTExMTExMS0yMjIyLTMzMzMtNDQ0NC01NTU1NTU1NTU1NTVAZXhhbXBsZS5jb206NDQz"
    got_b64 = parse_subscription(b64_body, "t")
    if len(got_b64) != 1:
        problems.append(f"base64 包体解析出 {len(got_b64)} 个，应为 1（nodes.txt / .txt 都是这种）")

    # 6. direct：订阅地址就是接口本身，**不许去抓页面**
    #    判据是「fetch 有没有被调用」—— 页面抓不到（改版 / Cloudflare 挑战）时
    #    连接口也一起没了，而接口才是唯一必要的东西。
    direct = [s for s in SOURCES if s.kind == "direct"]
    if not direct:
        problems.append("没有任何 direct 源（zmxoo 那种无扩展名的订阅接口）")
    else:
        saved = source_mod.fetch

        def _boom(url, **kwargs):
            raise AssertionError(f"direct 源不该抓页面：{url}")

        source_mod.fetch = _boom
        try:
            urls, _ = discover(direct[0], log=lambda *a, **k: None)
        except AssertionError as exc:
            urls = []
            problems.append(str(exc))
        finally:
            source_mod.fetch = saved
        if not urls or urls != list(direct[0].extra):
            problems.append(f"direct 源应原样返回 extra，实得 {urls}")

    # 7. dated-sub：数字文章 ID -> 正文里的 sub/clash-YYYYMMDD.yaml
    dated = [s for s in SOURCES if s.kind == "dated-sub"]
    if not dated:
        problems.append("没有任何 dated-sub 源（xinshoucun 那种纯数字文章 ID）")
    else:
        src = dated[0]
        pages = {
            src.url: XSC_INDEX,
            "https://xinshoucun.net/13783": XSC_ARTICLE,
            "https://xinshoucun.net/13780": "<p>这篇里没有订阅地址</p>",
        }
        saved = source_mod.fetch

        def _serve(url, **kwargs):
            if url in pages:
                return pages[url]
            raise source_mod.FetchError(f"404 {url}")

        source_mod.fetch = _serve
        try:
            got = _dated_subs(src, log=lambda *a, **k: None)
        finally:
            source_mod.fetch = saved
        want = "https://xinshoucun.net/sub/clash-20261024.yaml"
        if got != [want]:
            problems.append(f"dated-sub 应解析出 {want!r}，实得 {got}")
        # 必须取 ID 最大的那篇（导航里的 /40、/137 不该被当成文章）
        if got and "20261024" not in got[0]:
            problems.append(f"dated-sub 没取最新那篇（ID 最大），实得 {got}")

        # 文章页全都拿不到订阅地址时，退回按日期拼模板
        saved = source_mod.fetch

        def _only_index(url, **kwargs):
            if url == src.url:
                return XSC_INDEX
            if "/sub/clash-" in url:
                return "proxies: []\n"
            raise source_mod.FetchError(f"404 {url}")

        source_mod.fetch = _only_index
        try:
            fallback = _dated_subs(src, log=lambda *a, **k: None)
        finally:
            source_mod.fetch = saved
        if not fallback or "/sub/clash-" not in (fallback[0] if fallback else ""):
            problems.append(f"dated-sub 的日期兜底没生效：{fallback}")

    # 8. github-raw 的地址拼接：base 带尾斜杠 + extra 是相对路径
    for sid in ("cn-news", "sunmiao"):
        src = next((s for s in SOURCES if s.sid == sid), None)
        if src is None:
            problems.append(f"缺少源 {sid}")
            continue
        urls, _ = discover(src, log=lambda *a, **k: None)
        if not urls or not urls[0].startswith(src.url):
            problems.append(f"{sid} 的 raw 地址拼错了：{urls}")
        elif "//" in urls[0].split("://", 1)[1]:
            problems.append(f"{sid} 的 raw 地址出现了双斜杠：{urls[0]}")

    # 9. 自检必须探「流水线真正下载的那个地址」
    #    以前它从源码里正则抠 `"https://..."`，对 github-raw 抠到的是**目录**，
    #    而 GitHub raw 对目录回 404 —— 于是自检里那几个源永远 FAIL，
    #    真正的故障反而淹没在噪声里。判据：每个源恰好一个地址，且 github-raw
    #    的地址不能以 `/` 结尾（那就是目录）。
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "probe_env_under_test", ROOT / "scripts" / "probe_env.py"
        )
        probe_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe_mod)
        urls = probe_mod.source_urls()
        if len(urls) != len(SOURCES):
            problems.append(f"自检地址 {len(urls)} 个，源站 {len(SOURCES)} 个 —— "
                            f"要么被 limit 截断，要么漏了某个源")
        for source in SOURCES:
            if source.kind != "github-raw":
                continue
            want = source.url.rstrip("/") + "/" + source.extra[0]
            if want not in urls:
                problems.append(f"自检没探 {source.sid} 的真实文件地址 {want}")
        if any(u.endswith("/") and "raw.githubusercontent" in u for u in urls):
            problems.append(f"自检还在探 raw 的目录地址（必然 404）："
                            f"{[u for u in urls if u.endswith('/') and 'raw.githubusercontent' in u]}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"加载 scripts/probe_env.py 失败：{type(exc).__name__}: {exc}")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print(f"✓ 源站适配器通过：{len(sids)} 个源、CMS(.htm)/WP(日期目录)/direct(无扩展名接口)/"
          f"dated-sub(数字ID->带日期文件)/三种文章兜底/两种正文格式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
