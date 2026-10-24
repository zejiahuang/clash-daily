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
| ④ 延迟测速 | 内核真实过一遍节点，拿端到端延迟 | 约 200–400 条 |
| ⑤ 下载测速 | 每节点绑一个独立入站端口，并行下载 2 MB，取峰值 | 同时探测出口 IP |
| ⑥ 重命名 + 截断 | 按地区分组，组内按速度降序，再按国家配额取前 100 | `地区-序号-速度MB/S` |

### 为什么每次只留 100 个

`--max-nodes` 的截断走的是 `_stratified()`：先给每个国家分一份配额
（`100 // 国家数`），再用剩余名额按速度补齐。所以「100 个」不是「速度最快的 100 个」，
而是**先保证每个国家都有名额，再在名额内按速度排**。
如果只按速度取前 100，结果会退化成「测速机附近的 100 个」。

### 为什么地区要用出口 IP

源站节点名里的国旗**不可信**，而且大量节点套了 Cloudflare：
服务器 IP 在 `172.67.x.x`，GeoIP 查出来是美国，但实际出口可能在别处。
只有从节点里出去问一次 `api.ipify.org`，拿到的才是真实出口 IP。

### 为什么测速要按地区分层取样

直接取「延迟最低的前 N 个」等于只挑了离测速机最近的节点。
实测在美西机器上，最终 200 个节点里 179 个是美国（美国 10–40 ms，
亚洲 150–250 ms 排不到前面）。所以测速前按服务端 IP 归属国分桶分配名额。

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

# 完整跑一次
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
