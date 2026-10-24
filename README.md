# 节点聚合工具（每日自动测速）

每天自动抓取 4 个节点源站的最新数据，**真实下载测速**后按出口地区重命名，
产出可直接导入 Clash Verge / Mihomo / ClashX Meta 的配置文件。

```
采集 → 去重 → TCP 预筛 → 延迟测速 → 下载测速 → 出口地区识别 → 重命名 → 产出
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

想换成亚太视角，不需要改代码：在仓库 **Settings → Secrets and variables → Actions →
Variables** 新建一个变量 `RUNNER_LABEL`，值填你的 self-hosted runner 标签（如 `asia`）。
工作流里的 `runs-on` 会自动切过去。

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

## 四、流程细节

| 阶段 | 做什么 | 典型数量 |
|---|---|---|
| ① 采集 | 4 个源站的索引页 + 当天文章 + 直链 YAML | 约 2000–10000 条原始 |
| ② 解析去重 | `vmess/vless/trojan/ss/ssr/hysteria2/tuic` + Clash YAML，按 `(类型,服务器,端口,凭据)` 去重 | 约 2000 条 |
| ③ TCP 预筛 | 只保留 443/8443 等端口能建连的 | 约 1100 条 |
| ④ 延迟测速 | 内核真实过一遍节点，拿端到端延迟 | 约 200–400 条 |
| ⑤ 下载测速 | 每节点绑一个独立入站端口，并行下载 2 MB，取峰值 | 同时探测出口 IP |
| ⑥ 重命名 | 按地区分组，组内按速度降序 | `地区-序号-速度MB/S` |

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

## 五、本地调试

```bash
pip install -r requirements.txt

# 只验证采集与解析，不测速
python main.py --no-speedtest --max-nodes 50

# 完整跑一次
python main.py --out dist --work work --max-nodes 300 --speed-max 400

# 单元测试
python tests/test_parsers.py
python tests/test_bisect.py
python tests/test_selection.py
```

内核（mihomo）会自动下载；国内网络直连 GitHub 慢时，代码会依次尝试
`cdn.akaere.online`、`gh-proxy.com` 镜像。也可用 `MIHOMO_BIN` 指定本地已有内核。

---

## 六、产物

| 文件 | 说明 |
|---|---|
| `dist/clash.yaml` | 完整 Clash 配置（可直接导入） |
| `dist/report.md` | 本次构建报告：各阶段数量、地区分布、速度 Top N |
| `dist/report.json` | 同上，机器可读 |
| `dist/runner-geo.json` | 测速机出口 IP 与归属地（判断测速视角） |

---

## 七、平台选型记录（实测）

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
