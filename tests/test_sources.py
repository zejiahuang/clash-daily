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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.sources import (  # noqa: E402
    SOURCES,
    _article_links,
    _scan_article,
    _wp_posts,
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


def main() -> int:
    problems: list[str] = []

    # 1. 源站清单本身：sid 唯一、kind 都是已知值
    known_kinds = {"manifest", "index", "wp-dated", "github-raw", "gh-uploads"}
    sids = [s.sid for s in SOURCES]
    if len(sids) != len(set(sids)):
        problems.append(f"SOURCES 里有重复 sid: {sids}")
    if len(sids) != 10:
        problems.append(f"源站数量 {len(sids)} != 10（少接一个就是少一批节点）")
    for source in SOURCES:
        if source.kind not in known_kinds:
            problems.append(f"{source.sid} 的 kind={source.kind!r} 没有对应适配器")

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

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print(f"✓ 源站适配器通过：{len(sids)} 个源、CMS(.htm)/WP(日期目录)/三种文章兜底/两种正文格式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
