# 节点聚合工具（每日自动测速）

每天自动抓取 10 个节点源站的最新数据，**真实下载测速**后按出口地区重命名，
每次只保留 **100** 个节点，产出可直接导入 Clash Verge / Mihomo / ClashX Meta 的配置文件。

```
采集 → 去重 → TCP 预筛 → 延迟测速 → 下载测速 → 出口地区识别 → 重命名 → 取 100 → 产出
```

- 节点名格式：`日本-01-12.5MB/S`、`美国-03-8.4MB/S`、`中国香港-02-25.1MB/S`
  - 地区取自**实测出口 IP**，不是源站标注的国旗
  - 速度是**实测峰值下载速度**，不是源站宣称值
- 部署在 GitHub Actions，每天北京时间 02:00 自动更新

> 本项目仅用于网络协议与自动化运维的技术学习，不保证任何节点可用性，请遵守所在地法律法规。

---

## 一、订阅地址

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/dist/clash.yaml
```

国内直连 `raw.githubusercontent.com` 经常被中断，建议用任一加速前缀：

```
https://gh-proxy.com/https://raw.githubusercontent.com/<用户>/<仓库>/main/dist/clash.yaml
https://cdn.jsdelivr.net/gh/<用户>/<仓库>@main/dist/clash.yaml
```

---

## 二、测速视角（重要）

**速度是「测速机 → 节点」的速度，不是「你 → 节点」的速度。**
GitHub 托管的 `ubuntu-latest` 跑在美西，所以默认测出来的是美西视角。

工作流的第一步会把测速机的出口 IP 与归属地打印到日志，并写入
`dist/runner-geo.json`，方便核对本次结果是什么视角：

```json
{"status":"success","country":"United States","countryCode":"US",
 "regionName":"California","city":"San Jose","isp":"Microsoft Corporation"}
```

### 换成亚太视角（self-hosted runner）

需要一台香港 / 日本 / 新加坡的机器（1 核 2 GB 就够）。**代码一行不用改**，
工作流里的 `runs-on: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}` 会自动跟随仓库变量。

**① 取注册令牌**（有效期 1 小时）

```bash
curl -sX POST -H "Authorization: Bearer <你的 PAT>" \
  https://api.github.com/repos/zejiahuang/clash-daily/actions/runners/registration-token
```

也可以在仓库页面 **Settings → Actions → Runners → New self-hosted runner** 里取 `--token` 的值。

**② 在亚太机器上跑安装脚本**

```bash
RUNNER_TOKEN=<上一步拿到的令牌> bash scripts/install-runner.sh
```

脚本做四件事：装依赖（curl/tar/git/python3）、下载对应架构的 runner、
用标签 `asia` 注册、装成 systemd 服务并启动。

**③ 确认上线**

仓库 **Settings → Actions → Runners**，这台机器应显示 **Idle**，标签里能看到 `asia`。
**这一步没确认之前不要设变量** —— 设了但 runner 不在线，任务会一直排队直到超时。

**④ 打开开关**

**Settings → Secrets and variables → Actions → Variables** 新建 `RUNNER_LABEL`，值 `asia`。

**⑤ 验证视角真的变了**

手动跑一次，看「记录测速机位置」那一步输出的国家。改之前是
`United States / Virginia`，改成亚太 runner 后应该变成 `Hong Kong` / `Japan` / `Singapore`。
`dist/runner-geo.json` 里也有同样的记录。

> ⚠️ 公开仓库 + self-hosted runner 有已知风险：fork 发起的 PR 能在你的机器上执行任意代码。
> 本项目的工作流**只挂了 `schedule` 和 `workflow_dispatch`，没有 `pull_request`**，
> 所以外部无法触发。**后续不要给它加 PR 触发**，否则这台机器等于对外开放。

---

## 三、部署

### 1. 推送代码

```bash
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 2. 开启 Actions 写权限

**Settings → Actions → General → Workflow permissions** 选
**Read and write permissions**，否则最后一步无法把 `dist/` 提交回仓库。

### 3. 手动跑一次

**Actions → 每日节点聚合 → Run workflow**，参数保持默认即可。

---

## 四、源站清单

10 个源站，5 种页面结构，各配一个适配器（`clashhunter/sources.py`）：

| # | 源站 | 适配器 | 订阅地址从哪来 | 2026-10-24 实测 |
|---|---|---|---|---:|
| 1 | `manifest.dpdns.org/today/` | `manifest` | 页面里直接给直链 | 177 |
| 2 | `clash-node.com/free-node/` | `index` | 索引页 → 当日文章 → `/uploads/` | 4761 |
| 3 | `v2rayfree.org/` | `index` | 同上 | 2333 |
| 4 | `stelliberty.com/free-node/` | `index` | 同上 | 1092 |
| 5 | `lancex.net/free-node/` | `index` | 同上（文章是 `.htm`） | **0** ⚠️ |
| 6 | `nodetoolbox.net/free-nodes/` | `wp-dated` | WordPress 日期目录 → `nodesub.` 子域 | 30 |
| 7 | `clashsuburl.com/free-nodes/` | `wp-dated` | 同上 | 30 |
| 8 | `zhuhaiuk/free-nodes` | `github-raw` | 仓库固定文件名 raw | 45 |
| 9 | `TG-NAV/nodeshare` | `github-raw` | 自身产出为空，回退到它声明的上游 | 33 ⚠️ |
| 10 | `freeclashverge.github.io` | `gh-uploads` | `uploads/YYYY/MM/<n>-YYYYMMDD.yaml` 取最新日期 | 4769 |

合计 **13270 原始 → 2724 去重 → 2405 唯一端点 → 1311 通过 TCP 预筛 → 取 100**。

三个需要解释的地方：

**① `lancex` 抓到 0 个，是源站自己坏了，不是适配器写错。**
文章页能正常抓（43 KB），里面有 8 个订阅地址，但全部指向 `node.lancex.net`，
而该域**整站 526**（Cloudflare「源站证书无效」）—— 连根路径 `https://node.lancex.net/`
都是 526，带不带 Referer、http 还是 https、换哪一天的文章，结果都一样。
适配器保留着，源站修好会自动恢复；这期间只记一条 warn，不影响其它 9 个源。

**② `TG-NAV/nodeshare` 的产出长期是空的，所以配了兜底上游。**
它的 `clash.yaml` 和 `v2ray.txt` 都是 **0 字节**。看它自己的 `run.sh` 才知道原因：
内容靠 `api-suc.0z.gs` 这个 subconverter 生成，而该服务已经失效（实测连不上），
所以 workflow 每天跑、每天提交两个空文件。
兜底取的是它 `run.sh` 里自己声明的上游仓库 —— `Pawdroid/Free-servers`（可用）、
`ermaozi01/free_clash_vpn`（可用）、`free-nodes/clashfree`（已 404，跳过）。
**兜底只在本源产出为空时才用**，日志和 `report.md` 会写明「自身产出为空，已改用兜底上游」。

**③ `nodetoolbox` 和 `clashsuburl` 其实是同一份数据。**
两家订阅正文字节数完全相同（6516 B 的 `.yaml` / 4052 B 的 `.txt`），
解析出的 15 个节点也完全一致。不需要特殊处理 —— 去重会消掉一份。

### 源站改版了怎么办

每个源站在 `report.md` 里都有一行状态，三种状态要分清：

- `OK` —— 正常
- `失败：...` —— 适配器报错（索引页找不到文章链接 / 页面结构变了）
- `OK（找到 N 个订阅地址但全部下载失败）` —— 地址找到了，文件下不下来（`lancex` 目前是这种）
- `OK（自身产出为空，已改用兜底上游）` —— 见上面第 ② 条

---

## 五、流程细节

| 阶段 | 做什么 | 典型数量 |
|---|---|---|
| ① 采集 | 10 个源站，5 种页面结构 | 约 13000 条原始 |
| ② 解析去重 | `vmess/vless/trojan/ss/ssr/hysteria2/tuic` + Clash YAML，按 `(类型,服务器,端口,凭据)` 去重 | 约 2700 条 |
| ③ TCP 预筛 | 只保留 443/8443 等端口能建连的 | 约 1300 条 |
| ④ 延迟测速 | 内核真实过一遍节点，拿端到端延迟（只用于排序，**不用来砍数量**） | 约 700–900 条 |
| ⑤ 下载测速 | **全部延迟可用节点都测**，每节点绑一个独立入站端口，并行下载 2 MB，取峰值 | 同时探测出口 IP |
| ⑥ 复测 | 把初筛最快的 300 个用**低并发**重测一次，消除抢带宽造成的低估 | 300 条 |
| ⑦ 挑选 + 重命名 | 按实测速度排，再按国家配额取前 100 | `地区-序号-速度MB/S` |

### 为什么是「先全部测速，再挑」

早期版本在测速**之前**就按国家分层砍到 400 个（为了省时间）。这是个错误的设计：
**没被抽中测速的节点永远进不了最终名单**，等于用延迟提前决定了结果，
而延迟和下载速度并不相关。现在的顺序是：

```
延迟测速（全部） → 下载测速（全部） → 复测（最快的 300） → 按速度挑 100
```

`--speed-max` 仍然保留，但它现在是**可选的时间上限**，默认 `0`（不截断）。
只有手动传了 `--speed-max N` 才会在测速前截断，日志会写明「按国家分层截断后进入测速」。

### 为什么要两轮测速（初筛 + 复测）

初筛并发 32，几十个节点同时抢 runner 那一条出口带宽，**快节点会被压低**，
而写进节点名的就是这个数。所以初筛只负责「找出谁快」，再把最快的 300 个
用并发 16 重测一次，用复测结果覆盖初筛值。复测只覆盖「确实测出速度」的项 ——
复测失败不能把初筛的有效结果冲掉。

### 为什么每次只留 100 个

`--max-nodes` 的截断走的是 `_stratified()`：先给每个国家分一份配额
（`100 // 国家数`），再用剩余名额按速度补齐。所以「100 个」不是「速度最快的 100 个」，
而是**先保证每个国家都有名额，再在名额内按速度排**。
如果只按速度取前 100，结果会退化成「测速机附近的 100 个」。

> ⚠️ 配额的代价是可以量化的：run #7 有 49 个国家，配额 `100 // 49 = 2`，
> 于是最终 100 个里有 5 个低于 1 MB/S（喀麦隆 0.1 ×2、中国 0.1、越南 0.6、哈萨克斯坦 0.9）。
> 如果只想要速度最好的 100 个，把最终截断处换成纯速度排序即可（`builder.py` 第 6 步）。
> 现在是「每个国家 2 个保底 + 剩下的按速度补」。

### 0 速度的节点不会被收录

`speed = 0` 有两种含义：节点不可用，或者压根没被测到。两种写进名字里都是 `0.0MB/S`，
对使用者没有意义，所以最终挑选前会把它们丢掉（**因此最终可能少于 100 个**）。

这一条是实测出来的：测速前截断到 400 的那一版，最终 100 个里有 **21 个是 `0.0MB/S`**
（1546 个延迟可用节点里只测了 400 个，剩下的连机会都没有）。全量测速后这个数字应该趋近于 0。

### 同一个出口只算一个节点

`nodes.dedupe()` 的键是 `(type, server, port, 凭证)`，所以「同一台服务器、同一个端口，
只是凭证不同」的两条会被当成两个节点。实测 run #7 的最终 100 个里，有 **7 组共 14 条**
其实是同一个出口：

| 出口 IP | 两条节点 | 为什么没被去重键抓住 |
| --- | --- | --- |
| `138.199.35.215` | 美国-07(9.9) / 美国-13(9.6) | 同 `server:port`，凭证不同 |
| `213.183.63.60` | 保加利亚-01(2.8) / 保加利亚-02(2.6) | 同机不同端口 |
| `102.244.78.57` | 喀麦隆-01(0.1) / 喀麦隆-02(0.1) | 同机不同端口 |
| `185.135.86.175` | 拉脱维亚-01(3.0) / 拉脱维亚-02(2.9) | 同机不同端口 |
| `103.86.116.118` | 印度尼西亚-01(2.5) / 印度尼西亚-02(1.6) | 不同入口、同出口 |
| `62.197.156.21` | 斯里兰卡-01(2.8) / 斯里兰卡-02(2.5) | 一台中继一台落地 |
| `103.86.49.57` | 泰国-01(3.9) / 泰国-02(2.7) | 两条 CF 前置 |

同一个出口的带宽和故障是共享的，留两条等于把 100 个名额里的一个白扔。
所以测速完成后加了一步 `_dedupe_by_exit()`：**按出口 IP 分组，每组只留最快的那个**；
没有测出出口 IP 的，退到 `server:port` 作为键（实测 瑞士-01/瑞士-02 就是
`116.204.164.17:7443` 的两条，都没测出出口 IP，不做这个兜底就会被留下）。

这一步必须在**分层取样之前**做，否则腾出来的名额不会被别的出口用上。
用 run #7 的真实数据离线复现：`100 → 92`，正好合并掉上面那 8 个。

### 为什么地区要用出口 IP

源站节点名里的国旗**不可信**，而且大量节点套了 Cloudflare：
服务器 IP 在 `172.67.x.x`，GeoIP 查出来是美国，但实际出口可能在别处。
只有从节点里出去问一次 `api.ipify.org`，拿到的才是真实出口 IP。

判定顺序是 **出口 IP > 服务端 IP > 节点名里的地区标签**。所以测不出出口 IP 的节点，
国家是从服务端 IP 推的 —— run #7 的 100 个里有 7 个属于这种，它们的国家标签**不保证准确**。

⚠️ 出口 IP 的返回值必须校验。以前只判断「非空且短于 64 字节」，结果 run #7 的
加拿大-01 把 `104.26.0.0` 当出口 IP 交上来了 —— 那是 Cloudflare 的**网段地址**，
不是任何客户端的地址，GeoIP 照样给它编了个国家，于是节点名里的国家是错的。
现在 `_plausible_exit_ip()` 要求：能解析成 IP、是公网地址、IPv4 末位不是 `.0`/`.255`。

### 为什么延迟排序不再用来砍数量

早期用「延迟最低的前 N 个」当测速候选，等于只挑了离测速机最近的节点：
实测在美西机器上，最终 200 个节点里 179 个是美国（美国 10–40 ms，
亚洲 150–250 ms 排不到前面）。现在延迟只用来排序，国家多样性靠**最终挑选**那一步的
国家配额保证，而不是靠提前砍掉。

### 为什么下载测速要给每个节点绑独立端口

Clash 的 selector 是全局状态，切一次只能测一个节点，300 个串行要十几分钟。
mihomo 的 `listeners` 支持 `proxy: <节点名>`，可以给每个节点绑一个独立的
mixed 端口，于是几百个节点能同时测。已实测：端口 7911 的出口 IP 确实等于
它所绑定节点的出口 IP。

### 为什么坏条目要二分定位

只要有一条 proxy 写错，mihomo 就拒绝加载整份配置。
用二分而不是逐条试：200 条含 3 个坏条目，实测 15 次校验就能全部定位。

---

## 六、本地调试

```bash
pip install -r requirements.txt

# 只验证采集与解析，不测速
python main.py --no-speedtest --max-nodes 100

# 只跑某几个源（排查单个源站改版时用）
python main.py --no-speedtest --sources nodetoolbox,clashsuburl

# 完整跑一次（默认：全量测速、初筛 32 并发、复测最快的 300 个）
python main.py --out dist --work work --max-nodes 100

# 想省时间就手动加时间上限（会在测速前按国家分层截断）
python main.py --out dist --work work --max-nodes 100 --speed-max 400

# 单元测试
python tests/test_parsers.py
python tests/test_bisect.py
python tests/test_selection.py
python tests/test_sources.py
```

内核（mihomo）会自动下载；国内网络直连 GitHub 慢时，代码会依次尝试
`cdn.akaere.online`、`gh-proxy.com` 镜像。也可用 `MIHOMO_BIN` 指定本地已有内核。

---

## 七、产物

| 文件 | 说明 |
|---|---|
| `dist/clash.yaml` | 完整 Clash 配置（可直接导入） |
| `dist/report.md` | 本次构建报告：各阶段数量、地区分布、速度 Top N |
| `dist/report.json` | 同上，机器可读 |
| `dist/runner-geo.json` | 测速机出口 IP 与归属地（判断测速视角） |

---

## 八、平台选型记录（实测）

做过一轮国内平台调研，结论留在这里，避免以后重复踩：

| 平台 | 免费 CI | 定时 | 能当配置直链吗 |
|---|---|---|---|
| GitHub Actions | 2000 分钟/月 | ✅ | ✅ raw 直链 |
| Gitee | Gitee Go 需开通（有 200 分钟/仓库 + 500 分钟/月免费额度） | ✅ | ⚠️ raw 直链可用，但含 `vmess/vless/trojan` 的配置会被内容审核拦成 **451** |
| AtomGit | 免费 | ✅ | ❌ `/-/raw/` 返回前端页面，不是文件内容 |
| CNB（腾讯云） | 160 核时/月 | ✅ | ❌ 同上 |
| 阿里云效 Flow | 3000 核分/月 | ✅ | ❌ |
| 极狐 GitLab | 无免费额度 | — | ✅ |

两个关键实测：

1. **国内平台只有 Gitee 提供真正的 raw 文本直链**（`Content-Type: text/plain`）。
   AtomGit / CNB 的 `/-/raw/` 都返回 5–145 KB 的前端 HTML，Clash 拿到会解析失败。
2. **Gitee 会按文件内容做审核**：纯 `ss` 配置返回 200，一旦含
   `type: vmess` / `vless` / `trojan` 就返回 `451 The content may contain violation information`。
   所以 Gitee 不能作为真实产物的分发地址。

---

## 九、AtomGit 镜像（代码在，但**不能当订阅直链**）

镜像仓库：<https://atomgit.com/StriveGlory/daily-config>

### 为什么不能当订阅地址

2026-10-24 实测了 **12 种**可能的 raw 地址形式，**没有一种返回文件原文**：

| 地址形式 | 结果 |
|---|---|
| `atomgit.com/<o>/<r>/raw/main/<path>` | 200，`text/html` 5526 B（前端 SPA） |
| `atomgit.com/<o>/<r>/-/raw/main/<path>` | 200，前端 SPA |
| `atomgit.com/<o>/<r>/raw/branch/main/<path>` | 200，前端 SPA |
| `atomgit.com/<o>/<r>/blob_plain/main/<path>` | 200，前端 SPA |
| `raw.atomgit.com/<o>/<r>/main/<path>` | 200，前端 SPA |
| `gitcode.com/<o>/<r>/raw/main/<path>` | 200，前端 SPA |
| `raw.gitcode.com/<o>/<r>/raw/main/<path>` | 403 |
| `api.atomgit.com/api/v5/repos/<o>/<r>/raw/main/<path>` | 404 |
| `api.atomgit.com/api/v5/repos/<o>/<r>/download/<path>` | 404 |
| `api.atomgit.com/api/v5/repos/<o>/<r>/contents/<path>?ref=main` | 200，但返回 **JSON**（`encoding: base64`） |
| `.../contents/<path>?ref=main&raw=1` | 同上，仍是 JSON |
| `.../blobs/main/<path>/raw` | 404 |

**根因**：AtomGit 是纯前端渲染（Vue SPA，资源全在 `cdn-static.gitcode.com`），
服务端把所有 `/raw/` 路径都路由到前端入口，真正的文件内容由 JS 调
`/api/v5/.../contents/...` 拿 base64 再在浏览器里解码。
**服务端不存在任何「直接吐原文」的端点**，所以 Clash / 任何订阅客户端都拿不到。
唯一能取到原文的接口是 contents API，但它是 JSON 包着 base64 —— 客户端不认。

⇒ **订阅地址必须继续放在 GitHub**（`raw.githubusercontent.com`）。
AtomGit 这边只是**代码镜像 + 产物存档**，供人浏览，不能订阅。

### 一个反直觉的发现：AtomGit 不拦内容

Gitee 会按文件内容审核（含 `vmess/vless/trojan` 就 451），但 AtomGit **完全不拦**：
23 个文件全部上传成功，包括 `dist/clash.yaml`（含 vmess/vless/trojan 节点）
和 `uri.py`（源码里有 `vmess://` 字符串）。

但**仓库名会被拦**：`clash-daily` 报「项目名称,路径存在违规内容」，
换成 `daily-config` 才建成。所以仓库叫 `daily-config`，不是 `clash-daily`。

### 怎么同步过去

```bash
ATOM_TOKEN=<你的 AtomGit 个人访问令牌> python .local-tools/sync_atomgit.py
```

脚本逐文件比对内容（`GET /contents/{path}` 拿 sha + base64 内容），只传有变化的，
并把 `dist/` 里的最新产物一并带上。注意 AtomGit 的认证头是 **`PRIVATE-TOKEN`**
（`Authorization: token xxx` 会 401，`Authorization: Bearer xxx` 也行）。

---

## 十、在 AtomGit 跑 CI（为了「中国大陆视角」测速）

GitHub 托管 runner 在美东，测出来是美东视角。AtomGit 的 runner 在国内，
所以**真正值得在 AtomGit 跑的是测速那一步**，产物再推回 GitHub（因为 AtomGit 不能当直链）。

### 配置路径与语法（实测，不是猜的）

| 项 | 值 |
|---|---|
| 配置文件目录 | **`.gitcode/workflows/*.yml`**（不是 `.atomgit/workflows/`，也不是 `.github/workflows/`） |
| 托管 runner | `runs-on: default` == `[ubuntu-latest, x64, small]`（2 核 8 GB） |
| 可选规格 | `slim`(1C4G) / `small`(2C8G) / `medium`(4C16G) / `large`(8C32G) / `xlarge`(16C64G) / `2xlarge`(32C128G) |
| 标签写法 | 三段式 `[os, arch, flavor]`，如 `[ubuntu-latest, x64, medium]`；`arch` 可为 `arm64` |
| 预装 | Python 3.x、Node 18/20、Go 1.21+、Java 11/17/21、make/cmake/mvn/gradle/pip、git |
| 定时 | `on.schedule[].cron`，**POSIX cron，UTC 时间**，最短间隔 5 分钟 |
| 手动 | `on.workflow_dispatch` + `inputs`（支持 string/boolean/choice/number） |
| 检出代码 | `uses: checkout`（裸 action 名，不是 `actions/checkout@v4`） |
| 上下文 | `${{ atomgit.sha }}` / `${{ atomgit.event... }}`；环境变量 `ATOMGIT_*` |
| Secrets / Variables | `${{ secrets.X }}` / `${{ vars.X }}`，在仓库设置里配 |

> ⚠️ **前提：流水线是内测名额制**。官方文档「快速开始」第一行写着
> 「申请获取代码化流水线内测名额（内测名额有限，详情请咨询 AtomGit 客服）」。
> 没拿到名额的话，`.gitcode/workflows/` 里的文件**不会被识别**，仓库的「流水线」标签页也不会有记录。

### 两步走

**第一步：探测（`.gitcode/workflows/probe-runner.yml`）**

这一步必须先跑，因为它决定方案成不成立：

- runner 的出口 IP 与归属地 → 确认是不是国内视角
- **能不能访问 `api.github.com`** → 不能的话，产物根本推不回 GitHub，
  而 AtomGit 自己又不能当直链 ⇒ 整个方案作废
- 能不能下载 mihomo、能不能访问 10 个源站和测速端点

触发方式：推送这个文件本身（`paths` 过滤），或手动 `workflow_dispatch`。

**第二步：每日流水线（`.gitcode/workflows/daily-nodes.yml`）**

跑完 `main.py` 后调 `scripts/push_to_github.py` 把 `dist/` 写回 GitHub。需要两个配置：

| 类型 | 名称 | 值 |
|---|---|---|
| Secret | `GH_TOKEN` | 一个有 `repo` 权限的 GitHub 令牌 |
| Variable | `GITHUB_REPO` | `zejiahuang/clash-daily` |

**两者缺任一不会让流水线失败** —— `push_to_github.py` 会打印「跳过」并返回 0，
产物仍留在 AtomGit 仓库的 `dist/` 里（能看，不能订阅）。

### ⚠️ 两个 cron 会互相覆盖

GitHub 的 `daily-nodes.yml` 也是 UTC 18:00。如果 AtomGit 这边跑通了，
**必须把 GitHub 那个的 `schedule` 注释掉**（保留 `workflow_dispatch` 作兜底），
否则两边同时间各跑一次，谁后提交谁覆盖，测速视角会在「美东」和「国内」之间随机跳。

`scripts/push_to_github.py` 的提交信息带「AtomGit 测速结果」前缀，
`git log --oneline dist/` 能看出最后一次是谁写的。
