# 节点聚合工具（每日自动测速）

每天自动抓取 14 个固定节点源站，**并在采集阶段自动搜索 GitHub 上近期更新的订阅仓**
（搜到的节点与固定源走同一条去重 / TCP / 测速流程），真实下载测速后按出口地区重命名，
每次只保留 **80** 个节点，产出可直接导入 Clash Verge / Mihomo / ClashX Meta 的配置文件。

只有一种跑法（见 §三）：

```
你的服务器   采集 → 去重 → TCP 预筛 → 延迟测速 → 下载测速 → 出产物
             ├─ 本地 60066 端口对外提供订阅（见 §三.4）
             └─ git push dist 到 GitHub（纯仓库，不做测试，见 §四）
```

- 节点名格式：`日本-01-12.5MB/S·净85`、`美国-03-8.4MB/S·净80`
  - 地区取自**实测出口 IP**，不是源站标注的国旗
  - 速度是**实测平均下载速度**（不含握手时间，见 §六），不是源站宣称值
  - `·净NN` 是出口 IP 纯净度 0~100（越高越像干净住宅 IP，见 §六），
    查不到时**不写**这个后缀 —— 未知不等于满分
- **速度永远是「测速机 → 节点」**，也就是你这台服务器的视角。
- GitHub 只保存代码和 `dist/`，**不运行 Actions、不参与测速**。

> 本项目仅用于网络协议与自动化运维的技术学习，不保证任何节点可用性，请遵守所在地法律法规。

---

## 一、订阅地址

主用地址是**服务器自己的 60066 站点**（见 §三.4）：

```
http://<服务器IP>:60066/clash.yaml          # Clash / Mihomo
http://<服务器IP>:60066/clash.base64.txt    # Base64（部分客户端只认 base64）
http://<服务器IP>:60066/                    # 静态页（订阅地址、统计、源站状态）
```

> ⚠️ **端口上限是 65535，`66666` 不是合法端口**（`bind()` 会直接 `OverflowError`）。
> 本项目默认用 `60066`；想换端口见 §三.4。

GitHub 只是**备份**。如果要从 GitHub raw 取，地址是：

```
https://raw.githubusercontent.com/zejiahuang/clash-daily/main/dist/clash.yaml
https://raw.githubusercontent.com/zejiahuang/clash-daily/main/dist/clash.base64.txt
```

判据：响应必须是 `text/plain`，且字节数与服务器上 `dist/` 里那份一致。

---

## 二、测速视角（重要）

**延迟和速度都是「这台机器 → 节点」的实测值，不是「你 → 节点」。**

延迟测速和下载测速现在都在**同一台服务器**上跑，所以产物头部只有一个测速位置：

```
# 测速位置：中国·广东·广州（该速度是这台机器到节点的实测值，不代表你所在网络的速度）
```

- 机器在**国内**：延迟判据成立（「国内能不能连上」），但速度上限受这台机器的上行带宽限制。
- 机器在**境外**：速度更能分辨快慢，但延迟就不是「国内能不能用」这个问题的答案了。

### 想换视角怎么办

**换服务器 / 换机房，不是改代码。** 唯一要接受的是：这台机器的网卡上行带宽
就是所有节点速度的天花板 —— 一台 100 Mbps 的 VPS 会让每个节点都测不出超过约 12 MB/s。

> 历史：曾经把下载测速拆给 GitHub Actions，用来绕开 CNB runner 3~4 MB/s 的窄出口。
> 现在整条流水线都在服务器上，那套拆分（`candidates.json` / `speed.json` 交接）已经删除。

---

## 三、部署（服务器单机，唯一方式）

一条命令跑完：**采集 → 去重 → TCP 预筛 → 延迟测速 → 下载测速 → 出产物**，
全部在你这台服务器上。GitHub 只存代码和 `dist/`，不再运行任何测试。

> ⚠️ **同一个 IP 最多只探 6 个端口**（`--max-ports-per-server`，0 = 关闭）。
> 节点站里常有「一个 IP 挂几十上百个端口」的 dump，全探一遍在云厂商看来就是
> 端口扫描：实测某个 IP 被探了 129 个端口，InstaBox 直接触发滥用检测、
> 把整台机器锁了 24 小时。这个限制不是性能优化，是防封。

### 1. 安装

```bash
git clone https://github.com/zejiahuang/clash-daily.git /opt/clash-daily
cd /opt/clash-daily
sudo bash deploy/install.sh                 # 建 .venv、装依赖、跑单元测试、预下载内核、装 systemd 定时任务 + 订阅站点
sudo bash deploy/install.sh --no-service    # 只装依赖，不装定时任务
sudo RUN_AT=03:30 bash deploy/install.sh    # 换个时间（默认 02:00 北京时间）
sudo GITHUB_TOKEN=ghp_xxx bash deploy/install.sh   # 顺手配好 PUBLISH 用的推送凭据
```

安装器做四件事：`.venv` + `requirements.txt`、把 mihomo 内核提前下到
`work/bin/`（下载失败在安装阶段就暴露，而不是等凌晨两点跑一半才红）、写
`/etc/systemd/system/clash-daily.{service,timer}`、写 `clash-daily-web.service`
（60066 端口的订阅站点）。机器上没有 systemd 时它只打印 crontab，不强行改系统。

### 2. 跑

```bash
bash scripts/run_local.sh                     # 完整一轮，产物在 dist/
PUBLISH=0 bash scripts/run_local.sh           # 只跑，不回写远端
EXTRA_ARGS="--sources manifest,clash-node" bash scripts/run_local.sh
```

可调变量（`MAX_NODES`、`SPEED_WORKERS`、`REFINE_TOP` 等）全在
`scripts/run_local.sh` 的文件头，不需要改脚本。

定时任务：

```bash
sudo systemctl start clash-daily.service      # 立刻跑一轮
systemctl list-timers clash-daily.timer       # 下一次什么时候
journalctl -u clash-daily.service -n 200 --no-pager
```

| 路径 | 内容 |
|---|---|
| `dist/clash.yaml` / `dist/clash.base64.txt` | 订阅产物 |
| `dist/report.md` / `dist/report.json` | 本轮报告（各阶段过滤数、失败原因、纯净度） |
| `logs/run-*.log` | 每次运行的完整日志，保留最近 30 份 |
| `work/.run.lock` | 并发锁：上一轮没跑完时新一轮直接退出 0，不会两边抢内核 |

### 3. 发布（可选）

`PUBLISH=1`（默认）会在产物写完后 `git add dist` → commit → push 到 `origin`。
GitHub 在这里**只是仓库**：不触发 Actions，不做测速。推不上去时退出码为 1，
但 `dist/` 里的产物是好的 —— 订阅不会更新，这一点会明确写在日志里。

服务器上配一次凭据（token 别写进 remote URL，否则 `git remote -v` 会明文显示）。
`deploy/install.sh` 带 `GITHUB_TOKEN` 时会自动写好下面这份：

```bash
git config --local credential.helper 'store --file=.git-credentials'
printf 'https://x-access-token:<TOKEN>@github.com\n' > .git-credentials
chmod 600 .git-credentials
```

或者运行时给环境变量，`ci_commit.sh` 会用 `GITHUB_TOKEN` / `GH_TOKEN` 兜底重试：

```bash
PUBLISH=1 GITHUB_TOKEN=ghp_xxx bash scripts/run_local.sh
```

订阅地址直接用 §三.4 的 60066 站点即可，**完全不依赖 GitHub**；GitHub 只是备份。

### 4. 订阅站点（默认 60066 端口）

`deploy/install.sh` 会额外装一个 `clash-daily-web.service`，把 `dist/` 直接挂到 HTTP 上。
首页是自包含的静态页：订阅地址（带复制按钮）、本轮统计、出口地区分布、源站状态，
全部从 `dist/report.json` 现读。

```
http://<服务器IP>:60066/                    # 静态页
http://<服务器IP>:60066/clash.yaml          # Clash / Mihomo
http://<服务器IP>:60066/clash.base64.txt    # Base64
http://<服务器IP>:60066/report.md           # 本轮报告
```

- 服务**只读** `dist/`，每次请求现读 —— 每天流水线跑完立即生效，**不用重启服务**
- 只放行白名单里的产物文件，路径遍历一律 404（判据在 `tests/test_serve.py`）
- 换端口：`sudo WEB_PORT=8080 bash deploy/install.sh`；只想装定时任务：`--no-web`
- 手动起：`.venv/bin/python scripts/serve_dist.py --port 60066`
- 防火墙要单独放行：`sudo ufw allow 60066/tcp`，或
  `sudo firewall-cmd --add-port=60066/tcp --permanent && sudo firewall-cmd --reload`
- ⚠️ **端口上限是 65535**，`66666` 不是合法端口，绑定时会直接 `OverflowError`

> 这是「自己在服务器上分销」：订阅地址就是你服务器的地址，不依赖 CNB / GitHub 的 raw 直链。
> 代价是**服务器必须一直在线**，并且端口要对订阅客户端可达。

### 5. GitHub 只作为仓库

- 仓库里**没有** `.github/workflows`，也**没有** `.cnb.yml`：push 不会触发任何测试或构建。
- 原来的三段拆分（`--stage delay|speed|finish`、`candidates.json` / `speed.json` 交接、
  `workflow_dispatch` 令牌）已随流水线配置一起删除。
- GitHub 只保存代码、`dist/` 产物和提交历史；换机器时 clone 下来重新 `deploy/install.sh` 即可。

### 6. 服务器要求

- Linux x86_64 / arm64，Python ≥ 3.9（`deploy/install.sh` 用 `python3 -m venv`）
- **必须能直连外网**：mihomo 自己 dial 节点，HTTP 代理环境变量不参与测速。
  只有代理才能出网的环境会在 `[3/6] TCP 预筛` 处报「一个都不剩」，这是环境问题，不是代码问题
- 出口带宽就是所有节点速度的天花板：100 Mbps ≈ 12 MB/s，脚本会先做
  「出口管道自检」再按实测带宽压并发（见 §六），不会因为管子窄就整片超时
- 磁盘：内核约 60 MB + 中间配置；2 核 2 G 起，一轮通常十几分钟

---

## 四、GitHub 只作为仓库

GitHub 从现在起**只做两件事：存代码、存 `dist/` 产物**。不跑 Actions、不跑测速、
不接收任何构建触发。CNB 的流水线配置也已删除。

| 事情 | 在哪做 |
| --- | --- |
| 采集 / 去重 / TCP / 延迟 / 下载测速 / 出产物 | 服务器（`scripts/run_local.sh`，systemd timer 每天 02:00） |
| 对外提供订阅 | 服务器（`clash-daily-web.service`，60066 端口） |
| 保存代码与产物历史 | GitHub（纯仓库，push 即结束） |

### 令牌权限

只需要「写仓库」这一项：

- 细粒度 token：`Contents: Read and write`（只给这一个仓库即可）
- 经典 token：勾 `repo`

配置方式见 §三.3。token 不要提交进仓库，`.git-credentials` 已在 `.gitignore` 里。

### 为什么不把 GitHub 当测试平台

- 延迟测速的判据是「国内能不能连上」；GitHub runner 在境外，测出来的不是这个问题。
- Actions 的机器每天换，速度读数没有可比性；服务器固定，趋势才有意义。
- 订阅产物与测速必须在同一台机器上，否则「测出来 30 MB/s」和「用户手里 3 MB/s」
  之间的差异永远说不清。

> 历史细节（CNB / Gitee / AtomGit 的实测与失败原因）保留在 §九，仅作记录，不再维护。

## 五、源站清单

14 个固定源站（7 种页面结构，各配一个适配器），外加 **1 个动态源：GitHub 仓库自动搜索**。
两者都在 `[1/6] 采集` 这一步产出，之后走完全相同的去重 / TCP / 延迟 / 测速流程
（`clashhunter/sources.py`）：

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
| 11 | `hello-world-1989/cn-news` | `github-raw` | 仓库固定文件名 `clash-free.yaml` | 6 |
| 12 | `sunmiao4458/free-proxy-airport` | `github-raw` | 仓库固定文件名 `output/clash.yaml` | 23 |
| 13 | `zmxoo.xyz` | `direct` | 接口本身就是订阅（`tian.zmxoo.xyz/subapi`） | 130 |
| 14 | `xinshoucun.net` | `dated-sub` | 数字文章 ID → 正文里的 `sub/clash-YYYYMMDD.yaml` | 168 |

合计 **13270 原始 → 2724 去重 → 2405 唯一端点 → 1311 通过 TCP 预筛 → 取 80**。

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

**④ `zmxoo` 的订阅地址没有文件扩展名，所以 `manifest` 那个适配器看不见它。**
页面上是 `https://tian.zmxoo.xyz/subapi`（Clash）与 `/linkapi`（小火箭 base64），
而 `manifest` 的正则是 `\.(ya?ml|txt)` —— 匹配不上。于是新加了 `direct`：
`extra` 里直接写订阅地址，**连页面都不抓**。
为什么不抓页面：页面改版或被 Cloudflare 挑战时，连接口一起没了；而接口才是唯一必要的东西。
代价是端点变了不会自动跟随，但那种情况下 `manifest` 也一样抓不到，区别只是报错信息更清楚。

**⑤ `xinshoucun` 的文章 ID 是纯数字，没有日期，日期只在正文里。**
`/13783` 是 2026-10-24 那篇 —— 从「今天是几号」推不出「哪篇最新」，
所以 `dated-sub` 适配器走「索引页 → ID 最大的文章 → 正文里的 `sub/clash-20261024.yaml`」。
兜底再按日期拼 `sub/clash-{date}.yaml`（今天往前试 6 天），
因为文章给的日期是**源站自己说的**，比猜准。

**⑥ GitHub 自动搜索：不维护清单也能接上新出现的仓。**

固定清单只能覆盖「已经知道的那 14 个站」，而 GitHub 上每天都有新的免费节点镜像仓。
所以采集阶段会顺带做一次**仓库搜索**（默认 3 条查询，只取最近 30 天有推送、非 fork / 非归档的仓），
每个仓读一次 README 抠订阅地址；README 里没有就按 `clash.yaml` / `sub.yaml` / `nodes.txt`
等固定文件名去 raw 试。搜到的节点和固定源**同一条流水线**，没有任何特殊待遇。

- 只查**仓库**，不查代码：GitHub 的代码搜索未认证一律 401，仓库搜索未认证则是 10 次/分钟。
- README 优先走 `raw.githubusercontent.com`（不计 API 限额），取不到才退回 API。
- `GITHUB_SEARCH=0` 可整个关掉；`--sources` 指定具体源站时它也不会跑。
- 配 `GITHUB_TOKEN` / `GH_TOKEN` 可把限额从 60 次/时提到 5000 次/时（可选，不配也能跑）。
- 搜索失败 / 限额用完 / `api.github.com` 不可达：只记一条 `warn`，**不影响其它 14 个源**。

实测（2026-11-14）：8 个仓里 6 个解析出节点，共 9789 个原始节点；其中 1 个仓就贡献 9137 个。
多出来的数量会由去重与 TCP 预筛消化，但这一源的产出波动会比固定源大得多。

### 源站改版了怎么办

每个源站在 `report.md` 里都有一行状态，三种状态要分清：

- `OK` —— 正常
- `失败：...` —— 适配器报错（索引页找不到文章链接 / 页面结构变了）
- `OK（找到 N 个订阅地址但全部下载失败）` —— 地址找到了，文件下不下来（`lancex` 目前是这种）
- `OK（自身产出为空，已改用兜底上游）` —— 见上面第 ② 条

---

## 六、流程细节

| 阶段 | 做什么 | 典型数量 |
|---|---|---|
| ① 采集 | 14 个固定源站（7 种页面结构）+ GitHub 仓库搜索 | 约 13000 + 数千（动态）条原始 |
| ② 解析去重 | `vmess/vless/trojan/ss/ssr/hysteria2/tuic` + Clash YAML，按 `(类型,服务器,端口,凭据)` 去重 | 约 2700 条 |
| ③ TCP 预筛 | 只保留 443/8443 等端口能建连的；同一 IP 最多探 6 个端口（防端口扫描误判） | 约 1300 条 |
| ④ 延迟测速 | 内核真实过一遍节点，用 **HTTPS** 探测拿端到端延迟（只用于排序，**不用来砍数量**） | 约 700–900 条 |
| ⑤ 下载测速 | 先量 runner 出口管道，再据此定并发与下载量；**全部延迟可用节点都测**，每节点绑一个独立入站端口 | 同时探测出口 IP |
| ⑥ 复测 | 把初筛最快的 300 个用**低并发 + 24 MB + 多源取中位数**重测一次，同时消除抢带宽造成的低估、0.05 s 分母地板截断、单一源偏置 | 300 条 × 源数 |
| ⑦ 挑选 + 重命名 | 按实测速度排，再按国家配额取前 80；同时算出出口 IP 纯净度 | `地区-序号-速度MB/S·净NN` |

### 为什么是「先全部测速，再挑」

早期版本在测速**之前**就按国家分层砍到 400 个（为了省时间）。这是个错误的设计：
**没被抽中测速的节点永远进不了最终名单**，等于用延迟提前决定了结果，
而延迟和下载速度并不相关。现在的顺序是：

```
管道自检 → 延迟测速（全部，HTTPS 探测） → 下载测速（全部，参数由管道决定）
→ 源自检 → 复测（最快的 300，24 MB × 多源取中位数；窄管道下 24 个 × 1 MB × 单源）
→ 按速度挑 80
```

`--speed-max` 仍然保留，但它现在是**可选的时间上限**，默认 `0`（不截断）。
只有手动传了 `--speed-max N` 才会在测速前截断，日志会写明「按国家分层截断后进入测速」。

### 延迟测速必须用 HTTPS，不能用 HTTP 明文

`TEST_URL` 原来是 `http://www.gstatic.com/generate_204`。延迟测速走 HTTP 明文、
下载测速走 HTTPS —— **两个阶段测的根本不是同一件事**。

2026-10-24 那轮的数据：

| 阶段 | 结果 |
|---|---|
| 延迟可用（HTTP 明文探测） | 290 |
| 下载可用（HTTPS 下载） | 25 |
| 265 个的失败原因 | `SSL: UNEXPECTED_EOF_WHILE_READING` |

`UNEXPECTED_EOF_WHILE_READING` 是 **TLS 握手阶段就被关掉**，不是 HTTP 层的 403/421。
也就是说这 265 个节点能转发明文 HTTP，却连一个 HTTPS 站点都打不开 ——
对用户毫无价值（现代网页全是 HTTPS），却占满了第 ⑤ 步的名额和时间。

改成 `https://www.gstatic.com/generate_204` 之后，这批节点在延迟阶段就被筛掉：
候选从 290 降到几十个，第 ⑤ 步只剩真正能用的，顺带省掉它们后面各自的等待。

> ⚠️ 这个缺陷在日志里的样子是「延迟可用 290 → 下载可用 25」，看起来像「今天节点质量差」。
> 实际是**两个阶段的判据不一致**。凡是「A 阶段大面积通过、B 阶段大面积失败」，
> 第一件该做的事是问：A 和 B 测的是不是同一件事？

### 出口管道自检：并发和下载量由「管子有多粗」决定

第 ⑤ 步开始前会先量一次**这台服务器**的出口，再按量到的数决定实际并发与每节点下载量。
下面的数字来自早期 CNB runner（出口 3~4 MB/s），现在只作为「窄管道长什么样」的样本 ——
你的服务器越宽，自检后用的并发和下载量就越大，不需要手调。

`--speed-workers`（默认 32）和 `--speed-bytes`（默认 6 MB）都只是**上限**。

```
出口自检：单连接 3.41 MB/s，6 条并发合计 3.70 MB/s（6/6 条有数据，墙钟 6.0s）
   ⇒ 并发没有提升（1.1×），出口总带宽就是这么多
⚠️ 出口管道只有 3.70 MB/s（门槛 20）⇒ 32 并发 × 6.0 MB 改成 7 并发 × 256 KB
     每连接分到约 0.53 MB/s，这就是初筛读数的天花板；再快的节点要靠复测（1 并发独占管道）才能量出来
     并发不取 1：卡住的节点烧满超时且不占带宽，串行会把它们一个个等过去
```

（2026-10-24 在 CNB runner 上量到的历史读数，出口 111.230.93.5 广州腾讯云。
同一台机器当天几次采样是 3.70 / 3.66 / 2.79 / 4.15 / 4.34 MB/s —— 所以写死一个常数
没有意义，必须每轮当场量。）

> ⚠️ **只有并发总量能当管道用，单连接读数只能当噪声。** 那天五次采样的
> 「单连接 / 6 并发合计」是 **3.41/3.70、1.37/3.66、2.64/2.79、1.76/4.15、0.46/4.34**
> —— 并发总量稳定在 2.8~4.3，单连接却在 0.46~3.41 之间跳（7.4 倍，比值 1.09×~9.43×）。
> 0.46 那次不代表管子变窄：同一个 6 秒窗口里 6 条并发合计仍是 4.34。
> 所以 `_measure_pipe()` 把**并发总量**当管道交给 `_fit_to_pipe()`，
> 单连接读数只用来判断「是不是按连接限速」。

**单连接和并发总量必须分开量，不能拿一个当另一个。** 单连接慢有两种相反的成因：

| 观测 | 含义 | 该怎么改 |
|---|---|---|
| 并发总量 ≈ 单连接 | 出口总带宽就这么宽 | **必须**压并发和每节点下载量 |
| 并发总量 ≫ 单连接 | 只是每条连接被限速 | **不该**压，32 并发完全跑得动 |

哪个成立只能**在这台服务器上量**（`dist/probe.json` 的 `bandwidth` 字段）。
主机上量出来的不算：同一份脚本在本机跑，1 条连接 0.41 MB/s、6 条合计 5.22 MB/s
（12.7×），隔几分钟再跑 1 条连接只读到 64 KB —— 主机网络本身不稳，这个比值不能外推。

> ⚠️ **写进节点名的速度不会超过这台服务器的出口带宽。**
> 这是平台带宽，不是节点质量：一个 50 MB/s 的节点在 4 MB/s 的机器上也会读成 4。
> 想拿到更高的读数只能换一台出口更宽的服务器 —— 调参数是调不出来的。

窄管道（< 20 MB/s）的规则，每一条都是被实测改出来的：

- **并发 = 管道 / 0.5，下限 2、上限 `--speed-workers`。绝不能取 1。**
  超时是**延迟**不是带宽：卡住的节点一个字节都没下到，却要占满 10 秒。
  2026-10-24 那轮取 1 并发，**964 秒里有 480 秒**花在 50 个这样的节点上
  （50 × 10 s ÷ 1 = 500 s）。开 8 并发后同样的分布只要 ~63 秒。
- **每节点下载量 256 KB**（只往下压，不往上抬）。读数上限 `256K/0.05 = 5 MB/s`
  已高于这种管道的天花板，字节数越小墙钟越短。
- **复测不跳过，改成 1 并发 × 1 MB × 前 24 个。** 初筛为了摊掉超时只能开并发，
  读数被摊平到 `管道/并发`；要把真值量回来只能让复测独占管道。
  （上一版在这里跳过复测，理由是「管道比地板和抢带宽都窄」—— 那个理由只在
  初筛也用 1 并发时成立，而初筛用 1 并发正是产生那 8 分钟的那一版。）
- 多源取中位数在窄管道上关掉：要 ×源数 的时间，不划算。
- 量不出来（一个字节都没读到）⇒ **原样放行**，不猜。

管道自检的固定开销是两次测量（单连接 + 6 并发）× 6 秒封顶，所以最坏约 12 秒。
每次 `push` 触发的环境自检（`scripts/probe_env.py`）也会量一遍并写进 `dist/probe.json`
的 `bandwidth` 字段 —— 想看当天 runner 的管道宽度，不用等整轮跑完。


### 为什么要两轮测速（初筛 + 复测）

初筛并发 32，几十个节点同时抢 runner 那一条出口带宽，**快节点会被压低**，
而写进节点名的就是这个数。所以初筛只负责「找出谁快」，再把最快的 300 个
用并发 16 **并且每个下 24 MB（而不是初筛的 6 MB）**重测一次，用复测结果覆盖初筛值。
复测只覆盖「确实测出速度」的项 —— 复测失败不能把初筛的有效结果冲掉。

两个变量都要换，只换并发不够：字节数不换，读数仍然被分母下限截在 114.44 MB/s。
下一节给出算式。

### 写进名字的是**平均**速度，不是峰值

`measure_speed()` 返回 `总字节 / 实际传输时长`。关键是**分母从「收到第一个字节」算起**，
不从发请求算起 —— 从发请求算的话分母里含了 TCP+TLS 握手，而握手成本对每个节点是固定的，
于是它压低下所有节点的读数，且**速度越快压得越狠**：

| 节点真实速度 | 2 MB 下载耗时 | 握手 0.15 s 后报成 | 偏差 |
| ---: | ---: | ---: | ---: |
| 10 MB/s | 0.20 s | 5.7 MB/s | **−43%** |
| 1 MB/s | 2.00 s | 0.93 MB/s | −7% |

也就是说，把握手算进分母会把排名**系统性压平**，而用户最关心的恰恰是顶部那几个。
所以分母只取「首字节 → 末字节」，并设 0.05 s 下限（极快节点两三个 chunk 就收完 2 MB，
真实跨度只有几毫秒，不设下限会除出几十上百 MB/s 的假数字）。

平均值的代价是会比峰值低一截，但它更接近「持续下载一个大文件」的实际体验。

> ⚠️ **每个节点要下够字节，否则快节点会被分母下限截顶。** 0.05 s 下限意味着
> `每节点字节数 / 1048576 / 0.05` 就是能报出的上限：2 MB 时上限恰好是 **38.15 MB/s**。
> 这不是理论值 —— run #9 里就有 **5 个节点原始值完全相同、都是 38.15**，
> 来自五个不同机房，唯一解释是它们都撞在这个上限上，真实速度测不出来。
> 所以现在每节点下载 **6 MB**（上限 114.44 MB/s，38 MB/s 的节点能被测满 150 ms）。
> 可用 `--speed-bytes` 调；`scripts/run_local.sh` 里对应环境变量 `SPEED_BYTES`。
>
> 提高字节数的代价是流量与时间：1503 个节点 × 6 MB ≈ 9 GB/轮，主步骤耗时约 3 倍。
>
> ⚠️ **URL 里的字节数必须和 `max_bytes` 同源。** 这里踩过一次：`SPEED_URL` 写死了
> `?bytes=2000000`，把 `--speed-bytes` 改成 6 MB 之后服务端仍然只发 2 MB 就 EOF，
> 循环提前结束 —— **改动静默失效**，产物里照样是 38.15。现在 URL 是模板
> `...?bytes={bytes}`，由 `measure_speed()` 用 `max_bytes` 填。
> 判据：`tests/test_speed_math.py` 会断言请求 URL 里的 `bytes=` 跟着 `max_bytes` 变。

### 复测单独用 24 MB，初筛仍是 6 MB

6 MB 的上限是 114.44 MB/s —— **这个上限不是理论值，是撞上过的**。run #11 头名报
112.75 MB/s，反推分母 `5.722 / 112.75 = 0.0507 s`，正好压在 0.05 s 地板上。
也就是说那个数字**不是测出来的，是除出来的**：真实值可能是 150，也可能是 500。

所以复测（决定最终排名与名字里那个数）改用 **24 MB**：

| 阶段 | 每节点字节 | 分母上限 | 覆盖范围 |
| --- | ---: | ---: | --- |
| 初筛 | 6 MB | 114.4 MB/s | 全部（~1400 个，约 9 GB） |
| 复测 | 24 MB | **457.8 MB/s** | 最快的 300 个（上限 7.2 GB） |

24 MB 的上限已经高到免费节点够不着，地板从此不参与计算。初筛保持 6 MB 是因为
1400 × 24 MB = 33 GB，只为排个序不值得；而复测每个节点都被 `speed_timeout` 单独兜住，
慢节点不会把整轮拖长。

> ⚠️ **复测量必须大于初筛量。** 如果复测沿用 `speed_bytes`，等于白做一轮 ——
> 只是换了个低并发，读数照样被 114.44 截断。`tests/test_refine.py` 钉住了三件事：
> 复测传给 `probe_all` 的字节数是复测量、复测失败不冲掉初筛值、
> `build()` 与 `main.py` 的默认值不漂移。反证过：把复测换回初筛量，该测试立刻报错。
>
> 可用 `--refine-bytes` 调；`scripts/run_local.sh` 里对应 `REFINE_BYTES`。

### 复测对多个网站各测一遍，取中位数

单个源只回答「到这个站有多快」。一个节点到 Cloudflare 快、到 OVH 慢，是很常见的
——所以复测对**多个真实站点**各下一份数据，写进名字的是这些读数的**中位数**。

关键点：**用哪几个源不写死**。同一个机房到不同测速站的表现会随时间变
（早期 GitHub runner 在凤凰城 / 博伊德尔 / 昆西 / 得梅因 / 圣何塞都出现过），
某个源今天能不能跑满，事先不可能知道。所以每轮开跑前先做一次**直连自检**：

```
测速源自检（直连，不经节点，每源 24 MB）
  cloudflare      92.30 MB/s
  ovh             41.00 MB/s
  hetzner-ash      0.00 MB/s
未采用：hetzner-ash(不可用)、tele2(12.1)
⇒ 采用 2 个源取中位数：cloudflare / ovh
```

判据是 `max(20 MB/s, 最快那个 × --source-min-ratio)`：**一个源自己跑不到 20 MB/s，
经它测出来的节点读数就永远到不了 20** —— 那是源的天花板，不是节点的。低于门槛的
直接踢掉；全部低于门槛时至少留最快的那个（退回单源反而更差）。

> ⚠️ 自检是**串行**的。并行会让几个源互相抢 runner 的出口带宽，量到的就不是各自的容量了。
>
> ⚠️ 代价是流量：`--speed-sources 3` 时复测总流量是单源的 3 倍
> （300 个 × 3 × 24 MB ≈ 21.6 GB）。嫌多就调小，`0` 表示退回单源。

> ⚠️ **Cloudflare 的 `bytes` 参数有一段稳定 403 区间：`11,000,000 ≤ bytes < 20,000,000`。**
> 2026-10-24 实测，10 MB 以下与 20 MB 以上都是 200，落在中间全是 403。
> 而 `measure_speed()` 是 catch 后返回 0 的 —— 落进这一段就是**每个节点都测出 0，静默全灭**。
> 默认复测量 24 MB 在安全侧，但改这个值时必须先看自检表里 cloudflare 那一行是不是 0。

### 为什么每次只留 80 个

`--max-nodes` 的截断走的是 `_stratified()`：先给每个国家分一份配额
（`80 // 国家数`），再用剩余名额按速度补齐。所以「80 个」不是「速度最快的 80 个」，
而是**先保证每个国家都有名额，再在名额内按速度排**。
如果只按速度取前 80，结果会退化成「测速机附近的 80 个」。

> ⚠️ 配额的代价是可以量化的。run #7 是 49 个国家、取 100 个，配额 `100 // 49 = 2`，
> 于是最终 100 个里有 5 个低于 1 MB/S（喀麦隆 0.1 ×2、中国 0.1、越南 0.6、哈萨克斯坦 0.9）。
> 想完全按速度取，把最终截断处换成纯速度排序即可（`builder.py` 第 6 步）。

### 单国上限：为什么名额一少，美国就会占掉三分之一

配额是 `floor(limit / 国家数)`，国家数约 47 时：

| 最终名额 | 配额 | 保底占用 | 按速度补 | 美国实际 |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 2 | 94 | 6 | 14/100 = 14% |
| 80 | **1** | 47 | **33** | **30/80 = 37.5%** |

名额越少、配额越接近 1，越多名额落到「纯速度」上 —— 而测速机在美国，顶部全是美国节点。
这不是 bug，是配额算法的定义决定的。所以加了 `cap_per_country`（`--country-share`，默认 0.2）：
**单个国家最多占 `round(limit × 0.2)` 个**（80 个时是 16），超出的跳过、让给别的国家，
但**总数仍然补到 limit**。

用 run #9 的真实分布验算：80 个里美国 30 → 压到 16，空出的 14 个名额从其他国家按速度补。

> 候选总数 ≤ limit 时函数原样返回，上限不参与 —— 那种情况下「多留一个」总比「按上限砍掉」好。
> 上限真的把名额卡死（候选里没有更多国家可补）时会打 `[warn]`，不会静默少给。

### 0 速度的节点不会被收录

`speed = 0` 有两种含义：节点不可用，或者压根没被测到。两种写进名字里都是 `0.0MB/S`，
对使用者没有意义，所以最终挑选前会把它们丢掉（**因此最终可能少于 80 个**）。

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

### 速度是「测速机到节点」的单点值，所以头部会写清测速机在哪

同一批节点，从美西测和从香港测是两个完全不同的数。所以订阅文件头部带一行：

```
# 测速位置：美国·Phoenix（该速度是这台机器到节点的实测值，不代表你所在网络的速度）
```

这一行由 `main.py` 从 `<out>/runner-geo.json` 读出来（`scripts/run_local.sh` 在
`main.py` 之前用 ip-api 写下这个文件），也可以用 `--vantage` 手动指定。读不到就整行不出现，
不影响构建。`report.md` 里也有同一行。

> ⚠️ **想换视角只能换机器。** 视角 = 服务器所在位置，代码改不了。
> 选一台对你用户群体有代表性的机器（多数情况下是境内），并且接受
> **那台机器的上行带宽会成为所有节点速度的天花板**
> —— 一台 100 Mbps 的 VPS 会让每个节点都测不出超过约 12 MB/s。
> 所以这是「拿速度上限换视角正确」，不是纯赚。

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

### 出口 IP 纯净度：`·净NN` 是怎么来的

免费节点绝大多数是机房 IP，登录、注册、看流媒体时被拦是常态。所以每个节点名后面
带一个 **0~100 的纯净度**，直接告诉你「这个出口像不像一个干净的住宅 IP」：

```
韩国-01-20.0MB/S·净80       机房 80（欺诈 1）
俄罗斯-02-7.4MB/S·净100     ISP  100（欺诈 0）
爱沙尼亚-01-13.4MB/S·净47   机房 47（欺诈 53）
```

**算法**（`clashhunter/purity.py`，纯函数，`tests/test_purity.py` 逐组合钉住）：

| 信号 | 来源 | 扣分 |
|---|---|---|
| 被标为代理 / VPN | ip-api `proxy` | −25 |
| 机房 IP（datacenter） | ip-api `hosting` | −20 |
| 移动网络（CGNAT 共享出口，滥用率高） | ip-api `mobile` | −5 |
| 第三方欺诈分 `N` | Scamalytics 的 `Fraud Score` | 取 `min(分数, 100 − N)` |

**为什么不是直接跑 [xykt/IPQuality](https://github.com/xykt/IPQuality)**：
它每个 IP 要打 8 个风险库、7 个流媒体、11 家邮局、400+ 个 DNSBL，单 IP 一分钟起步；
每天要标 80 个节点就是一小时以上。其中 6 个库还需要 API key，
而公开仓库里一条密钥都不放。所以只取**无密钥、单 IP 亚秒级**的两路，
判据仍然是 IPQuality 那三块里的「基础信息 / IP 类型 / 风险评分」。

> ⚠️ **`100` 分的准确含义是「这两路都没标记它」，不是「这是住宅 IP」。**
> ip-api 对一个 IP 返回 `hosting=false, proxy=false` 时我们无从知道它是家宽还是漏标；
> 实测 19 个真实节点里就有 2 个落在这一档。想要更硬的结论只能上多库交叉验证，
> 那就回到「需要密钥 + 单 IP 一分钟」的老问题上。
>
> ⚠️ **未知 ≠ 满分。** ip-api 没返回这个出口 IP 时 `score` 是 `null`，
> 节点名里**不写**后缀，`report.json` 里 `purity.known=false`。
> 写 `·净100` 或 `·净?` 都会让人把「没查到」当成「很干净」。

两路信号的成本差得很远，所以分开控制：

- **ip-api 那一路是白拿的** —— `geoip.lookup_batch()` 本来就要查这批出口 IP 的归属地，
  `proxy/hosting/mobile` 只是同一次 batch 请求里多要三个字段，不多发请求、不多占配额。
- **Scamalytics 那一路要限流** —— 每个 IP 抓一次 35 KB 的页面。所以 `--fraud-top`
  默认 **80**（只给最终产物那批，不给候选池几百个），并带 **90 秒总预算**、
  单请求不重试、解析不出就当没有。抓不到时分数退回只看 IP 类型，
  **不会挡住出产物**（`--fraud-top 0` 可以整段关掉）。

这条链有没有跑通，看两个地方：`dist/probe.json` 的 `purity` 字段（两个接口从 CNB
能不能打到、`hosting` 有没有正确返回），以及 `report.md` 的「出口 IP 纯净度」一节。


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

### 为什么没有「亚太视角交叉验证」（已删除）

曾经做过一版：挑一台亚太节点当跳板，用 `dialer-proxy` 串成
「本机 → 跳板 → 节点 → 目标站」，把「美西快、亚太慢」的节点找出来。
**两轮实测都没拿到可信数据，代码已整体删除**（`_apac_check`、`APAC_CODES`、
`--apac-*` 三个参数、`tests/test_apac.py`，以及内核里的 `hop` 分支）。

失败原因值得记下来：**跳板的瓶颈是上行带宽，不是它的下载速度。**
数据流是「目标节点 → 跳板 → 本机」，跳板要把收到的再上传一次，而机场节点的上行
普遍远低于下行。实测跳板直连 5.1 MB/s 时，经它测的节点没有一个超过 1.0；
换成 11.1 MB/s 的跳板后，40 个样本里 27 个完全测不出。链路本身就是天花板，
读数被压平之后「节点慢」和「链路慢」分不开 —— 判据再精巧也没用。

真正的解法只有一条：**测速机本身就在那个位置**（这也是现在整条流水线
都放在你自己服务器上的原因），而不是在测速机上再挂一跳。

---

## 七、本地调试

```bash
pip install -r requirements.txt

# 只验证采集与解析，不测速
python main.py --no-speedtest --max-nodes 80

# 只跑某几个源（排查单个源站改版时用）
python main.py --no-speedtest --sources nodetoolbox,clashsuburl

# 本机一条龙（测速也在这台机器上做 —— 服务器上跑的就是这条路）
python main.py --out dist --work work --max-nodes 80
bash scripts/run_local.sh          # 同一件事，外加日志 / 锁 / 校验 / 回写

# 想省时间就手动加时间上限（会在测速前按国家分层截断）
python main.py --out dist --work work --max-nodes 80 --speed-max 400

# 复测下载量可以单独调（必须大于 --speed-bytes，否则快节点被分母地板截断）
python main.py --out dist --work work --speed-bytes 6000000 --refine-bytes 40000000

# 复测只用一个源（省流量，但回到「只代表到那个站有多快」）
python main.py --out dist --work work --speed-sources 0

# 纯净度：默认给最终 80 个抓 Scamalytics 欺诈分（90 秒预算，抓不到就降级）
python main.py --out dist --work work --fraud-top 0     # 只留 ip-api 那一路，零额外请求

# 单元测试
python tests/test_parsers.py
python tests/test_bisect.py
python tests/test_speed_math.py
python tests/test_pipe.py
python tests/test_refine.py
python tests/test_selection.py
python tests/test_sources.py
python tests/test_purity.py
python tests/test_web.py
python tests/test_ci_commit.py
python tests/test_local.py
python tests/test_serve.py
```

> 全部测试都可以用 `for f in tests/test_*.py; do python3 "$f"; done` 一把跑完；
> 新增测试文件不用改任何流水线（GitHub 已经不跑 Actions 了，测试只在本地/服务器跑）。

`.cnb.yml` 已经删除；服务器上的定时任务直接用 `bash scripts/run_local.sh`。

内核（mihomo）会自动下载：先问 GitHub API 拿最新 tag，再依次试
`github.com` 直连、`cdn.akaere.online`、`gh-proxy.com` 三个地址。
也可用 `MIHOMO_BIN` 指定本地已有内核、用 `MIHOMO_TAG=v1.19.33` 钉死版本。

> ⚠️ **查不到 tag 不会让流水线失败**：`_latest_tag()` 的最后一条退路是
> `mihomo.PINNED_TAG`（当前 `v1.19.33`，实测 `mihomo -v` 就是这个版本）。
> 没有这条退路时，国内 runner 上 `api.github.com` 一不通就「拼不出任何下载地址」，
> 整条流水线会在采集阶段直接失败。
>
> 采集用的 GitHub 源站（`raw.githubusercontent.com`）同理：直连失败会自动换
> 镜像前缀重试，`GH_MIRROR_PREFIX=""` 可关闭，判据在 `tests/test_web.py`。

---

## 八、产物

| 文件 | 说明 |
|---|---|
| `dist/clash.yaml` | 完整 Clash 配置（可直接导入） |
| `dist/report.md` | 本次构建报告：各阶段数量、地区分布、速度 Top N、**出口 IP 纯净度** |
| `dist/report.json` | 同上，机器可读；每条 `nodes[].purity` 带 `known/kind/score/fraud/proxy/hosting/mobile/isp/as` |
| `dist/runner-geo.json` | 测速机出口 IP 与归属地（判断测速视角） |
| `dist/probe.json` | **只在 `push` 触发时生成**：runner 环境自检（含 `purity` 字段，见 `scripts/probe_env.py`） |

---

## 九、平台选型与迁移记录（实测）

做过两轮调研，结论留在这里，避免以后重复踩。**第二轮的结论推翻了第一轮的一条。**

| 平台 | 免费 CI | 定时 | 能当配置直链吗 |
|---|---|---|---|
| **CNB（腾讯云）← 当前** | 160 核时/月 | ✅ `crontab:` | ✅ **`/-/git/raw/<分支>/<路径>`** |
| GitHub Actions | 2000 分钟/月 | ✅ | ✅ raw 直链（但 runner 在美西，视角不对） |
| Gitee | Gitee Go 需开通 | ✅ | ⚠️ 直链可用，但含 `vmess/vless/trojan` 会被审核拦成 **451** |
| AtomGit | 免费（内测名额制） | ✅ | ❌ 12 种地址形式全试过，没有一种返回原文 |
| 阿里云效 Flow | 3000 核分/月 | ✅ | ❌ |
| 极狐 GitLab | 无免费额度 | — | ✅ |

### ⚠️ 曾经写错的一条：CNB 能不能当直链

第一轮调研记的是「CNB 的 `/-/raw/` 返回前端页面 ⇒ 不能当直链」。**这是测错了路径得出的结论。**

CNB 的文件直链是 **`/-/git/raw/<分支>/<路径>`**，不是 `/-/raw/`。2026-10-24 逐条实测
（被测文件是仓库里 14 字节的 `requirements.txt`）：

| 地址形式 | 结果 |
|---|---|
| `.../-/raw/main/requirements.txt` | 200，`text/html` 131848 B（前端「Page not found」页） |
| `.../-/raw/main/requirements.txt?inline=true` | 同上 |
| `.../-/raw/main/requirements.txt?download=true` | 同上 |
| `.../-/blob/main/requirements.txt?raw=true` | 200，`text/html` 206483 B |
| `.../raw/main/requirements.txt` | **404** |
| **`.../-/git/raw/main/requirements.txt`** | **200，`text/plain` 14 B —— 就是文件原文** |

订阅文件的判据：`.../-/git/raw/main/dist/clash.yaml` 返回 `200 text/plain`、34777 B，
与仓库里那份逐字节一致。

> 教训：**「某个平台不支持 X」这种结论，必须把「试过的地址形式」和「返回的 Content-Type /
> 字节数」一起记下来。** 只写一句「返回前端页面」，下一次就没法判断是平台不行还是路径不对 ——
> 这一次正是靠「12 种形式 + 状态码 + 大小」才敢回头重测，并且发现结论是错的。

### Gitee 的另一条实测

**Gitee 会按文件内容审核**：纯 `ss` 配置返回 200，一旦含 `type: vmess` / `vless` / `trojan`
就返回 `451 The content may contain violation information`。所以它不能作为真实产物的分发地址。

### AtomGit 为什么彻底放弃

镜像仓库曾是 <https://atomgit.com/StriveGlory/daily-config>。它的问题是**结构性的**：
纯前端渲染（Vue SPA），服务端把所有 `/raw/` 路径都路由到前端入口，真正的文件内容由 JS 调
`/api/v5/.../contents/...` 拿 base64 再在浏览器里解码 —— **服务端不存在「直接吐原文」的端点**，
所以 Clash / 任何订阅客户端都拿不到。12 种地址形式的实测结果保留在 git 历史里
（`git log -p --follow README.md`），`.gitcode/` 工作流和 `scripts/push_to_github.py` 一起删掉了。

顺带一条反直觉的发现：**AtomGit 不拦内容**（23 个文件全部上传成功，包括含 vmess 的
`dist/clash.yaml`），但**仓库名会被拦** —— `clash-daily` 报「项目名称,路径存在违规内容」，
换成 `daily-config` 才建成。

### 迁移到 CNB 时改了什么

| 项 | 之前（GitHub） | 现在（CNB） |
|---|---|---|
| CI 配置 | `.github/workflows/daily-nodes.yml` | `.cnb.yml` |
| 手动触发 | `workflow_dispatch` | `.cnb/web_trigger.yml` 里的按钮（`web_trigger_daily`） |
| 定时 | `cron: "0 18 * * *"`（UTC） | `"crontab: 0 2 * * *"`（Asia/Shanghai） |
| 提交回写 | workflow 内联 `git push` | `scripts/ci_commit.sh`（三个入口共用） |
| 产物校验 | workflow 内联 heredoc | `scripts/verify_dist.py` |
| 看不到日志时 | `upload-artifact` + 步骤摘要 | `push` 触发的 `dist/probe.json` 环境自检 |
| 测速视角 | 美西（runner 所在地） | **中国大陆** |

> ⚠️ **两个 cron 会互相覆盖**：CNB 的 `0 2`（Asia/Shanghai）等于 GitHub 的 `0 18`（UTC），
> 是**同一时刻**。迁移时 GitHub 的 `schedule` 已经注释掉；如果哪天两边都开，
> 谁后提交谁覆盖，测速视角会在两个地区之间随机跳。

### 最终形态：服务器自己跑，GitHub 只存产物

2026-11-14 起，GitHub 也不再作为测试平台：

| 项 | 现在 |
|---|---|
| 采集 / 测速 / 出产物 | 你的服务器（`scripts/run_local.sh`，systemd timer） |
| 订阅分发 | 同一台服务器的 60066 端口（`clash-daily-web.service`） |
| 代码 / 产物备份 | GitHub（纯仓库，push 即结束，不触发任何流程） |
| 已删除 | `.cnb.yml`、`.cnb/`、`.github/workflows/speedtest.yml`、`--stage delay\|speed\|finish`、`scripts/gh_dispatch.sh`、`scripts/wait_speed.sh`、`clashhunter/speedjob.py` |

上面这些 CNB / Gitee / AtomGit 的表格仅作选型记录。现在换平台只需要换一台服务器，
不再需要重新适配任何 CI。
