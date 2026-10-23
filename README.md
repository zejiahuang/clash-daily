# 免费节点每日聚合订阅

每天自动抓取 4 个免费节点源站的最新 Clash 订阅，**真实下载测速**后按出口国家重命名，
产出可直接导入 Clash Verge / Mihomo / ClashX Meta 的订阅文件。

- 采集 → 去重 → TCP 预筛 → 延迟测速 → **下载测速** → 出口国家识别 → 重命名 → 出订阅
- 全流程在 GitHub Actions 上跑，**每天北京时间 02:00** 自动更新
- 节点名格式：`日本-01-12.5MB/S`、`美国-03-8.4MB/S`、`中国香港-02-25.1MB/S`
  - 国家取自**实测出口 IP**，不是节点名里的国旗
  - 速度是**实测峰值下载速度**，不是源站宣称值

> 本项目仅用于网络协议与自动化运维的技术学习，不保证任何节点可用性，请遵守所在地法律法规。

---

## 一、部署（三步）

### 1. 建一个仓库并推上去

```bash
cd clash-daily
git init -b main
git add .
git commit -m "init: daily node aggregator"
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 2. 开启 Actions 写权限

`Settings → Actions → General → Workflow permissions` → 选 **Read and write permissions** → Save。

工作流用的是仓库自带的 `GITHUB_TOKEN`，**不需要额外配置任何 PAT / Secret**。

### 3. 手动跑一次验证

`Actions → 每日节点聚合 → Run workflow`。第一次跑会顺带下载 mihomo 内核，
大约 10–20 分钟。跑完后仓库里会多出 `dist/` 目录。

之后每天 02:00（北京时间）自动执行。

---

## 二、怎么用这个订阅

产物提交回仓库后，用 raw 地址订阅即可：

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/dist/clash.yaml
```

如果 raw 访问不畅，可换 CDN 前缀（jsDelivr 是缓存，可能滞后）：

```
https://cdn.jsdelivr.net/gh/<你的用户名>/<仓库名>@main/dist/clash.yaml
```

`dist/` 里四个文件：

| 文件 | 用途 |
| --- | --- |
| `clash.yaml` | 主订阅，Clash Verge / Mihomo / ClashX Meta 直接导入 |
| `clash.base64.txt` | base64 版订阅，v2rayN / Shadowrocket 等用 |
| `report.md` | 本次构建报告：各阶段过滤数量、地区分布、最快的 15 个节点 |
| `report.json` | 同上，机器可读，含每个节点的原始名、延迟、**峰值速度**、出口 IP |

---

## 三、工作原理

```
4 个源站（manifest / clash-node / v2rayfree / stelliberty）
   │  索引页 → 当日最新文章 → /uploads/*.yaml 与 *.txt
   ▼
① 采集       Clash YAML 与 vmess/vless/trojan/ss/ssr/hysteria2/tuic 分享链接统一解析
   ▼
② 去重       key = (类型, 服务器, 端口, 凭据)，约 10000 → 约 2500
   ▼
③ TCP 预筛   只测去重后的 (host, port)，把死地址砍掉，约 2500 → 约 1150
   ▼
④ 延迟测速   内核通过每个节点发一次 HTTP 请求，拿端到端延迟，约 1150 → 约 230
   ▼
⑤ 下载测速   每个节点独立端口，并行下载 2MB，算峰值速度 + 问出口 IP
   ▼
⑥ 重命名     按国家分组，组内按速度降序 → 国家-序号-速度MB/S
```

### 为什么第 ④ 步必须用内核测

TCP 能连上 443 **不代表**这个节点能用。免费节点里大量是"端口开着但协议是错的、
凭据已失效、SNI 被拒"。只有让代理内核真的通过它发一次请求，才能区分。

### 为什么第 ⑤ 步要「每节点一个端口」

Clash 的 selector 是全局状态，切一次只能测一个节点，300 个节点串行要十几分钟。
mihomo 的 `listeners` 支持 `proxy: <节点名>`，可以给每个节点绑一个独立的 mixed 端口，
于是几百个节点能同时测。

这条不是推测：实测给节点 A 绑 7911 端口后，从 7911 发请求拿到的出口 IP 正是 A 的出口 IP。

### 为什么国家要用出口 IP

源站节点名里的国旗**不可信**，而且大量节点套了 Cloudflare 这类 CDN：
服务器 IP 在 `172.67.x.x`，GeoIP 查出来是美国，但实际出口可能在别处。
只有从节点里出去问一次 `api.ipify.org`，拿到的才是真实出口 IP。

---

## 四、常用参数

```bash
python main.py --out dist                     # 完整跑
python main.py --no-speedtest --max-nodes 50  # 只验证采集链路（本地没网时用）
python main.py --sources manifest,clash-node  # 只跑部分源站
python main.py --top-per-country 20           # 每个国家最多留 20 个
python main.py --max-nodes 500 --speed-max 600
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--max-nodes` | 300 | 最终保留多少节点 |
| `--speed-max` | 400 | 最多对多少个节点做下载测速（按延迟从快到慢取） |
| `--speed-bytes` | 2000000 | 每个节点下载多少字节用于测速 |
| `--speed-timeout` | 10 | 单节点下载测速超时（秒） |
| `--speed-workers` | 16 | 下载测速并发，**调太高会互相抢带宽，测出来偏低** |
| `--delay-workers` | 32 | 延迟测速并发 |
| `--delay-timeout` | 5000 | 单节点延迟超时（毫秒），越小越快但会误杀慢节点 |
| `--tcp-timeout` | 3.0 | TCP 预筛超时（秒） |
| `--max-articles` | 3 | 每个索引站回看几篇（当天没抓到就往前翻） |
| `--no-speedtest` | 关 | 跳过测速，只跑采集 |

环境变量：

- `MIHOMO_BIN` — 指定已有的 mihomo 可执行文件，跳过下载
- `MIHOMO_URL` — 指定内核下载地址（默认依次试 GitHub / `cdn.akaere.online` / `gh-proxy.com`）

---

## 五、源站

| 源站 | 形态 |
| --- | --- |
| `manifest.dpdns.org/today/` | 页面直接给出 `free/latest/clash.yaml` |
| `clash-node.com/free-node/` | 索引页 → 当日文章 → `node.clash-node.com/uploads/*` |
| `v2rayfree.org/` | 同上（`node.v2rayfree.org`） |
| `stelliberty.com/free-node/` | 同上（`node.stelliberty.com`） |

后三个是同一套 CMS 模板，共用一个适配器。每个源站都会把当日文章里列出的
**所有** `.yaml` 与 `.txt` 订阅文件都抓下来（通常各站 4–6 个），不会只取一个。

源站改版时只需要改 `clashhunter/sources.py` 里的 `_HREF_RE` / `_UPLOAD_RE`。

⚠️ 这几个源站对裸 `urllib` 会返回 **403**，必须带浏览器 UA 和 Referer——
`clashhunter/web.py` 里已经处理。

---

## 六、目录结构

```
clash-daily/
├── main.py                      # CLI 入口
├── requirements.txt             # 只有一个依赖：PyYAML
├── clashhunter/
│   ├── web.py                   # HTTP（UA/Referer/重试/gzip）
│   ├── uri.py                   # 分享链接 → Clash proxy 字典
│   ├── nodes.py                 # 规范化、校验、去重
│   ├── sources.py               # 源站发现与订阅解析
│   ├── netcheck.py              # TCP 预筛
│   ├── mihomo.py                # 内核下载、配置校验、延迟测速、并行下载测速
│   ├── geoip.py                 # 域名解析 + ip-api 批量归属地
│   ├── countries.py             # alpha-2 → 中文名
│   └── builder.py               # 流水线编排与产物生成
├── tests/
│   ├── test_parsers.py          # 11 种协议形态 + 边界
│   └── test_bisect.py           # 二分定位坏条目
└── .github/workflows/daily-nodes.yml
```

---

## 七、已知限制

1. **节点存活率低**。免费源站本身就不稳定，一天能活下来的通常只有采集量的
   10%–20%，这是数据本身的性质，不是脚本的问题。
2. **测速受运行环境影响**。GitHub 的 runner 在海外，测出来的速度普遍比国内直连乐观；
   同一个节点换网络环境结论会变。
3. **峰值速度有抖动**。取的是 0.25 秒窗口的最大值，节点抖动大时同一天不同时段
   跑出来的数字可能差一倍。
4. **CDN 节点会成组重复**。同一个 Cloudflare IP 上挂着十几个不同域名的节点，
   去重只能按 (服务器, 端口, 凭据) 判断，所以会出现多个只差 `servername` 的节点。
5. **定时任务可能延迟**。GitHub 的高峰期排队会让 02:00 的任务晚几分钟到几十分钟。
6. **分支保护会挡住自动提交**。如果 `main` 开了保护规则，需要允许
   `github-actions[bot]` 推送，否则最后一步会失败（产物仍可在 Actions 的 artifact 里下载）。

---

## 八、许可

仅供学习研究，自负风险。
