"""用 mihomo（Clash.Meta 内核）做真实代理测速与出口 IP 探测。

为什么不用"TCP 连接耗时"当测速结果：能连上 443 不代表这个节点能用。
mihomo 的 /proxies/{name}/delay 是让内核真的通过该节点发一次 HTTP 请求，
返回的是端到端可用延迟——这是唯一有意义的判据。

一个坑：只要有一条 proxy 写错，mihomo 就拒绝加载整份配置。
所以用「二分定位坏条目」的办法，而不是逐条试（1900 条逐条试太慢）。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote

import yaml

from .web import FetchError, fetch, fetch_json

API_HOST = "127.0.0.1"
API_PORT = 9090
MIXED_PORT = 7891
GROUP = "PROBE"
TEST_URL = "http://www.gstatic.com/generate_204"


# --------------------------------------------------------------------------- 下载内核


def _asset_candidates() -> list[tuple[str, str]]:
    """返回候选 (资源名后缀, 压缩格式)。mihomo 同时发布 -compatible 与普通版，逐个试。"""
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
        data = fetch_json("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
                          headers={"Accept": "application/vnd.github+json"})
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
                    name = next(n for n in zf.namelist() if n.endswith(".exe"))
                    data = zf.read(name)
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


# --------------------------------------------------------------------------- 内核封装


class Mihomo:
    def __init__(self, binary: Path, workdir: Path, *, log=print):
        self.binary = binary
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.proc: subprocess.Popen | None = None
        self._config_checks = 0

    # ---------------------------------------------------------------- 配置
    def _config(self, proxies: list[dict]) -> dict:
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
        return doc

    def _write_config(self, proxies: list[dict], name: str = "config.yaml") -> Path:
        path = self.workdir / name
        path.write_text(
            yaml.safe_dump(self._config(proxies), allow_unicode=True, sort_keys=False),
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
    def start(self, proxies: list[dict]) -> None:
        path = self._write_config(proxies, "run.yaml")
        # stderr 一定要落到文件：用 PIPE 而不读，缓冲区（64KB）写满后内核会卡死。
        self._log_path = self.workdir / "mihomo.log"
        log_file = open(self._log_path, "wb")
        self.proc = subprocess.Popen(
            [str(self.binary), "-d", str(self.workdir), "-f", str(path)],
            stdout=log_file, stderr=log_file,
        )
        self._log_file = log_file
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log_file.close()
                detail = self._log_path.read_text("utf-8", "replace") if self._log_path.exists() else ""
                raise RuntimeError(f"mihomo 启动即退出: {detail[-800:]}")
            try:
                with socket.create_connection((API_HOST, API_PORT), timeout=1):
                    time.sleep(0.6)
                    return
            except OSError:
                time.sleep(0.4)
        self.stop()
        raise RuntimeError("mihomo 控制端口 45s 内未就绪")

    def stop(self) -> None:
        if not self.proc:
            return
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
            handle = getattr(self, "_log_file", None)
            if handle and not handle.closed:
                handle.close()

    # ---------------------------------------------------------------- API
    def _api(self, path: str, method: str = "GET", payload=None, timeout: float = 20):
        url = f"http://{API_HOST}:{API_PORT}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, body
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

    def http_via_proxy(self, url: str, timeout: float = 8.0) -> str | None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({
                "http": f"http://{API_HOST}:{MIXED_PORT}",
                "https": f"http://{API_HOST}:{MIXED_PORT}",
            })
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "clash-daily/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                return resp.read(200).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return None
