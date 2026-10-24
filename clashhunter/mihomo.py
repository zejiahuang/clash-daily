"""用 mihomo（Clash.Meta 内核）做真实代理测速。

两条关键设计，都是踩过坑之后定下来的：

1. **测速必须让内核真的过一遍节点**。TCP 能连上 443 不代表节点可用——免费节点里
   大量是"端口开着但凭据失效 / SNI 被拒"。只有让代理内核通过它发一次真实请求，
   才能区分。所以用 mihomo 的 /proxies/{name}/delay 拿端到端延迟。

2. **下载测速用「每节点一个入站端口」而不是全局选择器**。
   Clash 的 selector 是全局状态，切一次只能测一个节点，300 个节点串行要十几分钟。
   mihomo 的 `listeners` 支持 `proxy: <节点名>`，可以给每个节点绑一个独立的
   mixed 端口，于是几百个节点能同时测（已实测：端口 7911 的出口 IP 确实等于
   绑定节点的出口 IP）。这是并行测速的前提。

另一个坑：只要有一条 proxy 写错，mihomo 就拒绝加载整份配置，
所以用二分定位坏条目，而不是逐条试。
"""

from __future__ import annotations

import gzip
import io
import ipaddress
import json
import os
import platform
import re
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import yaml

from .web import FetchError, UA, fetch_json

API_HOST = "127.0.0.1"
API_PORT = 9090
MIXED_PORT = 7891
GROUP = "PROBE"
TEST_URL = "http://www.gstatic.com/generate_204"
# ⚠️ 这里的字节数是**模板占位符**，由 measure_speed() 用 max_bytes 填。
# 曾经写成固定的 `?bytes=2000000`，结果把 --speed-bytes 改成 6 MB 之后
# 服务端仍然只发 2 MB 就 EOF，total 永远到不了 6 MB —— 改动静默失效，
# 产物里 5 个节点照样卡在 38.15 的封顶值上。URL 与 max_bytes 必须同源。
SPEED_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
LISTENER_BASE_PORT = 20000

# 多目标可达性探测。主延迟只用 TEST_URL（Google）一个目标，而「能通 Google」
# 不等于「能通别的站」—— 有些免费节点专门针对 Google 优化过。
# 这几个都是标准的 captive-portal 检测端点，全球可用、返回体极小。
#
# ⚠️ 为什么不换测速目标：实测从 CI 测 speedtest.net 的日本服务器只有 1.07 MB/s，
# 而 Cloudflare 是 7.0 MB/s。差的那 6 倍是测速服务器自己的带宽上限，不是节点的问题
# —— 拿它当测速目标会系统性低估所有节点。Cloudflare 是唯一带宽够高的目标。
REACH_TARGETS = (
    ("google", "http://www.gstatic.com/generate_204"),
    ("cloudflare", "http://cp.cloudflare.com/generate_204"),
    ("microsoft", "http://www.msftconnecttest.com/connecttest.txt"),
    ("apple", "http://captive.apple.com/hotspot-detect.html"),
)


def _plausible_exit_ip(text: str) -> bool:
    """出口 IP 探测的返回值是不是一个可信的「我这边的地址」。

    以前只判断「非空且短于 64 字节」，结果一个 http 节点把 `104.26.0.0` 当出口 IP
    交上来了（run #7 的 加拿大-01）—— 那是 Cloudflare 的网段地址，不是任何客户端
    的地址，GeoIP 照样给它编了个国家，于是节点名里的国家是错的。
    代理返回错误页时，短响应体很容易被当成 IP，所以这里必须真的解析一遍。

    排除项：
    - 不是合法 IP 字面量（含 HTML 片段、域名等）
    - 私有 / 回环 / 链路本地 / 保留地址 —— 那是隧道内部地址，不是出口
    - IPv4 末位是 .0 或 .255 —— 网段地址与广播地址，ipify 不会把它报成客户端地址
    """
    if not text or len(text) >= 64:
        return False
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if not addr.is_global:
        return False
    if addr.version == 4 and int(str(addr).rsplit(".", 1)[1]) in (0, 255):
        return False
    return True


# --------------------------------------------------------------------------- 下载内核


def _asset_candidates() -> list[tuple[str, str]]:
    """候选 (资源名后缀, 压缩格式)。mihomo 同时发布 -compatible 与普通版，逐个试。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "linux":
        return [(f"linux-{arch}-compatible", "gz"), (f"linux-{arch}", "gz")]
    if system == "darwin":
        return [(f"darwin-{arch}", "gz")]
    if system == "windows":
        return [(f"windows-{arch}", "zip"), (f"windows-{arch}-v3", "zip")]
    raise FetchError(f"不支持的平台: {system}/{machine}")


def _latest_tag() -> str | None:
    try:
        data = fetch_json(
            "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
        )
        tag = data.get("tag_name")
        if tag:
            return tag
    except Exception:  # noqa: BLE001
        pass
    # 退路：/releases/latest 会 302 到 /releases/tag/<tag>，urlopen 跟随重定向后能拿到最终地址
    try:
        req = urllib.request.Request(
            "https://github.com/MetaCubeX/mihomo/releases/latest",
            headers={"User-Agent": "clash-daily/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            final = resp.geturl()
        match = re.search(r"/releases/tag/([^/?#]+)", final)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_binary(dest_dir: Path, *, log=print) -> Path:
    """把 mihomo 可执行文件准备好，返回路径。可用 MIHOMO_BIN 直接指定。"""
    override = os.environ.get("MIHOMO_BIN")
    if override and Path(override).exists():
        log(f"  使用 MIHOMO_BIN={override}")
        return Path(override)

    dest_dir.mkdir(parents=True, exist_ok=True)
    exe_name = "mihomo.exe" if platform.system().lower() == "windows" else "mihomo"
    target = dest_dir / exe_name
    if target.exists() and target.stat().st_size > 1_000_000:
        log(f"  复用已下载内核 {target}")
        return target

    urls: list[str] = []
    env_url = os.environ.get("MIHOMO_URL")
    if env_url:
        urls.append(env_url)
    tag = _latest_tag()
    if tag:
        for sfx, fmt in _asset_candidates():
            name = f"mihomo-{sfx}-{tag}.{fmt}"
            urls.append(
                f"https://github.com/MetaCubeX/mihomo/releases/download/{tag}/{name}"
            )
            # 国内网络直连 GitHub 常常很慢，这两个镜像实测可用
            urls.append(
                "https://cdn.akaere.online/https://github.com/MetaCubeX/mihomo/"
                f"releases/download/{tag}/{name}"
            )
            urls.append(
                f"https://gh-proxy.com/https://github.com/MetaCubeX/mihomo/"
                f"releases/download/{tag}/{name}"
            )

    last_error: Exception | None = None
    for url in urls:
        try:
            log(f"  下载内核 {url}")
            raw = _download_bytes(url)
            if url.endswith(".gz"):
                data = gzip.decompress(raw)
            elif url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    inner = next(n for n in zf.namelist() if n.endswith(".exe"))
                    data = zf.read(inner)
            else:
                data = raw
            target.write_bytes(data)
            target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            log(f"  内核就绪 {target} ({len(data) / 1048576:.1f} MB)")
            return target
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log(f"    [warn] 失败: {type(exc).__name__}: {exc}")
    raise FetchError(f"mihomo 下载失败: {last_error}")


def _download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "clash-daily/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


# --------------------------------------------------------------------------- 内核封装


class Mihomo:
    def __init__(self, binary: Path, workdir: Path, *, log=print):
        self.binary = binary
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.proc: subprocess.Popen | None = None
        self._log_path = workdir / "mihomo.log"
        self._log_file = None
        self._config_checks = 0

    # ---------------------------------------------------------------- 配置
    def _config(self, proxies: list[dict], listener_ports: dict[str, int] | None = None) -> dict:
        doc: dict = {
            "mixed-port": MIXED_PORT,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "silent",
            "ipv6": False,
            "unified-delay": True,
            "external-controller": f"{API_HOST}:{API_PORT}",
            "proxies": proxies,
        }
        if proxies:
            doc["proxy-groups"] = [
                {"name": GROUP, "type": "select", "proxies": [p["name"] for p in proxies]}
            ]
            doc["rules"] = [f"MATCH,{GROUP}"]
        else:
            # mihomo 会拒绝 `use` 和 `proxies` 都为空的 select 组
            # （"proxy group[0]: PROBE: `use` or `proxies` missing"），
            # 空配置自检必须走这条分支，否则启动前的健康检查就会失败。
            doc["rules"] = ["MATCH,DIRECT"]
        if listener_ports:
            doc["listeners"] = [
                {
                    "name": f"L{index:04d}",
                    "type": "mixed",
                    "port": port,
                    "listen": "127.0.0.1",
                    "proxy": name,
                }
                for index, (name, port) in enumerate(listener_ports.items())
            ]
        return doc

    def _write_config(self, proxies: list[dict], name: str = "config.yaml",
                      listener_ports: dict[str, int] | None = None) -> Path:
        path = self.workdir / name
        path.write_text(
            yaml.safe_dump(
                self._config(proxies, listener_ports), allow_unicode=True, sort_keys=False
            ),
            encoding="utf-8",
        )
        return path

    def config_ok(self, proxies: list[dict]) -> bool:
        self._config_checks += 1
        path = self._write_config(proxies, "check.yaml")
        try:
            proc = subprocess.run(
                [str(self.binary), "-t", "-d", str(self.workdir), "-f", str(path)],
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            return False
        if proc.returncode == 0:
            return True
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        if detail:
            self.log(f"    [config] 拒绝: {detail[-1][:160]}")
        return False

    def filter_loadable(self, proxies: list[dict]) -> tuple[list[dict], list[dict]]:
        """二分找出会让 mihomo 拒绝加载的坏条目。

        必须用「左半 -> 右半」的递归而不是栈：栈的弹出顺序会让 good 列表的
        顺序和输入不一致，节点顺序就变得不确定了（tests/test_bisect.py 守着这点）。
        """
        if not proxies:
            return [], []
        if not self.config_ok([]):
            raise RuntimeError("空配置都无法通过 mihomo 校验，内核或环境有问题")

        good: list[dict] = []
        bad: list[dict] = []
        budget = {"left": 4000}

        def bisect(chunk: list[dict]) -> None:
            if budget["left"] <= 0:
                # 预算耗尽：整块判坏（宁可少几个节点，也不要无限跑下去）
                bad.extend(chunk)
                return
            budget["left"] -= 1
            if self.config_ok(chunk):
                good.extend(chunk)
                return
            if len(chunk) == 1:
                bad.append(chunk[0])
                return
            mid = len(chunk) // 2
            bisect(chunk[:mid])
            bisect(chunk[mid:])

        bisect(proxies)
        self.log(f"  配置校验 {self._config_checks} 次：可加载 {len(good)}，坏条目 {len(bad)}")
        return good, bad

    # ---------------------------------------------------------------- 生命周期
    def start(self, proxies: list[dict], listener_ports: dict[str, int] | None = None,
              ready_port: int | None = None) -> None:
        path = self._write_config(proxies, "run.yaml", listener_ports)
        # 输出必须落文件：用 PIPE 而不读，缓冲区写满后内核会卡死。
        self._log_file = open(self._log_path, "wb")
        self.proc = subprocess.Popen(
            [str(self.binary), "-d", str(self.workdir), "-f", str(path)],
            stdout=self._log_file, stderr=self._log_file,
        )
        probe_port = ready_port or MIXED_PORT
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._log_file.close()
                detail = self._log_path.read_text("utf-8", "replace") if self._log_path.exists() else ""
                raise RuntimeError(f"mihomo 启动即退出: {detail[-800:]}")
            try:
                with socket.create_connection((API_HOST, probe_port), timeout=1):
                    time.sleep(0.3)
                    return
            except OSError:
                time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"mihomo 端口 {probe_port} 60s 内未就绪")

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.proc = None
        if self._log_file and not self._log_file.closed:
            self._log_file.close()

    # ---------------------------------------------------------------- API
    def _api(self, path: str, method: str = "GET", payload=None, timeout: float = 20):
        url = f"http://{API_HOST}:{API_PORT}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception:  # noqa: BLE001
            return 0, b""

    def delay(self, name: str, url: str = TEST_URL, timeout_ms: int = 5000) -> int | None:
        path = (
            f"/proxies/{quote(name, safe='')}/delay"
            f"?url={quote(url, safe='')}&timeout={timeout_ms}"
        )
        status, body = self._api(path, timeout=timeout_ms / 1000 + 10)
        if status != 200:
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        value = data.get("delay") or data.get("meanDelay")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        return None

    def select(self, name: str, group: str = GROUP) -> bool:
        status, _ = self._api(
            f"/proxies/{quote(group, safe='')}", method="PUT", payload={"name": name}
        )
        return status in (200, 204)

    # ---------------------------------------------------------------- 单节点探测
    @staticmethod
    def _opener(port: int):
        proxy = f"http://{API_HOST}:{port}"
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )

    def exit_ip(self, port: int, timeout: float = 6.0) -> str | None:
        for url in ("http://api.ipify.org", "http://icanhazip.com"):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with self._opener(port).open(req, timeout=timeout) as resp:
                    text = resp.read(200).decode("utf-8", "replace").strip()
                if _plausible_exit_ip(text):
                    return text
            except Exception:  # noqa: BLE001
                continue
        return None

    def measure_speed(self, port: int, url: str = SPEED_URL,
                      max_bytes: int = 6_000_000, timeout: float = 10.0) -> float:
        """下载一段固定大小的数据，返回**平均**速度（MB/s）。失败返回 0。

        ⚠️ 平均值的分母必须从「收到第一个字节」算起，不能从发请求算起。
        从发请求算的话分母里含了 TCP+TLS 握手，而握手成本对每个节点是**固定**的
        —— 于是它压低下所有节点的读数，且**速度越快压得越狠**：
        2 MB 的下载在 10 MB/s 上只要 0.2 s，握手 0.15 s 就能把 10 报成 5.7；
        而 1 MB/s 的节点只是从 1.0 变成 0.93。这会系统性地把排名压平。

        分母下限取 0.05 s：极快的节点可能在两三个 chunk 内就把数据收完，
        真实跨度只有几毫秒，不设下限会除出几十上百 MB/s 的假数字。
        """
        # URL 里的 {bytes} 必须跟着 max_bytes 走，否则「下够多少」和「服务器发多少」
        # 会对不上：服务器先 EOF，循环提前结束，改 max_bytes 等于没改。
        if "{bytes}" in url:
            url = url.replace("{bytes}", str(max_bytes))
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
        )
        started = time.time()
        total = 0
        first_byte_at = 0.0
        last_byte_at = 0.0
        try:
            with self._opener(port).open(req, timeout=timeout) as resp:
                while total < max_bytes:
                    chunk = resp.read(min(65536, max_bytes - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    last_byte_at = time.time()
                    if not first_byte_at:
                        first_byte_at = last_byte_at
                    if last_byte_at - started > timeout:
                        break
        except Exception:  # noqa: BLE001
            pass
        if total < 65536 or not first_byte_at:
            return 0.0
        span = max(last_byte_at - first_byte_at, 0.05)
        return (total / span) / 1048576

    def probe_one(self, port: int, *, speed_url: str, speed_bytes: int,
                  speed_timeout: float, ip_timeout: float) -> dict:
        """先测速再测出口 IP：速度 0 说明节点不可用，不必再花时间问 IP。

        `ip_timeout <= 0` 表示跳过出口 IP（交叉复测时用：出口 IP 上一轮已经拿到，
        再问一次纯属浪费）。
        """
        speed = self.measure_speed(port, speed_url, speed_bytes, speed_timeout)
        exit_ip = None
        if speed > 0 and ip_timeout > 0:
            exit_ip = self.exit_ip(port, ip_timeout)
        return {"speed": speed, "exit_ip": exit_ip}

    def probe_all(
        self,
        proxies: list[dict],
        *,
        speed_url: str = SPEED_URL,
        speed_bytes: int = 6_000_000,
        speed_timeout: float = 10.0,
        ip_timeout: float = 6.0,
        workers: int = 16,
        batch_size: int = 150,
        base_port: int = LISTENER_BASE_PORT,
        hop: dict | None = None,
        log=print,
    ) -> dict[str, dict]:
        """分批给每个节点绑一个入站端口，并行测速 + 探出口 IP。

        `hop` 非空时，每个被测节点都挂上 `dialer-proxy: <hop 名>`，链路变成
        本机 → hop → 节点 → 目标站。hop 自己不进 ports，所以不占监听端口。

        ⚠️ 串起来的链路速度取「最慢的一段」，而 `本机 → hop` 这一段就在路径里
        ⇒ **hop 自身的速度就是整轮测速的天花板**。它只适合回答
        「这个节点从 hop 所在地区看是不是至少 X MB/s」，不能用来测出高于 hop 的速度。
        """
        results: dict[str, dict] = {}
        total = len(proxies)
        if not total:
            return results
        hop_name = hop["name"] if hop else None
        if hop_name and any(p["name"] == hop_name for p in proxies):
            raise ValueError(f"跳板 {hop_name} 不能同时出现在被测节点里（会自我循环）")
        log(f"  下载测速：{total} 个节点，并发 {workers}，"
            f"每个最多下 {speed_bytes / 1e6:.1f}MB，超时 {speed_timeout:.0f}s"
            + (f"，经跳板 {hop_name}" if hop_name else ""))

        done = 0
        for offset in range(0, total, batch_size):
            targets = proxies[offset:offset + batch_size]
            # 端口只按被测节点分配；跳板没有 listener，不占端口
            ports = {p["name"]: base_port + i for i, p in enumerate(targets)}
            if hop:
                # 每批都要把跳板重新放进 proxies —— mihomo 的 dialer-proxy 引用的是
                # 同一份配置里的 proxy 名，跨批不共享。
                batch = [hop] + [{**p, "dialer-proxy": hop_name} for p in targets]
            else:
                batch = targets
            try:
                self.start(batch, ports, ready_port=base_port)
            except Exception as exc:  # noqa: BLE001
                log(f"    [warn] 批次启动失败（跳过 {len(targets)} 个）: {exc}")
                continue
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            self.probe_one, ports[p["name"]],
                            speed_url=speed_url, speed_bytes=speed_bytes,
                            speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                        ): p["name"]
                        # 遍历 targets 而不是 batch：batch 里那个跳板没有端口
                        for p in targets
                    }
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            results[name] = future.result()
                        except Exception:  # noqa: BLE001
                            results[name] = {"speed": 0.0, "exit_ip": None}
                        done += 1
                        if done % 25 == 0 or done == total:
                            alive = sum(1 for v in results.values() if v["speed"] > 0)
                            log(f"    ... {done}/{total}，可用 {alive}")
            finally:
                self.stop()
        return results
