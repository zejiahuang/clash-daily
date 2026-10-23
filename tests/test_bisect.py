"""二分定位算法的回归测试。

背景：mihomo 只要有一条 proxy 写错就拒绝加载整份配置。1900 条逐条试太慢，
所以用二分。这个算法一旦写错，表现是"全部节点被判定为坏"或"坏条目漏进配置"，
而这两种情况在真实环境里都很难一眼看出来——所以单独测。

不需要真的内核：把 config_ok 换成"读回 YAML 看看有没有 BAD 服务器"。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from clashhunter.mihomo import Mihomo  # noqa: E402


def make_proxy(index: int, bad: bool = False) -> dict:
    return {
        "name": f"n{index:04d}",
        "type": "ss",
        "server": f"BAD-{index}.example.com" if bad else f"ok{index}.example.com",
        "port": 443,
        "cipher": "aes-128-gcm",
        "password": "pw",
        "udp": True,
    }


CHECKS = {"n": 0}


def fake_config_ok(self, proxies):
    """模拟 mihomo -t：写配置再读回来，出现 BAD 服务器就判失败。"""
    CHECKS["n"] += 1
    path = self._write_config(proxies, "check.yaml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if proxies:
        assert doc["proxy-groups"][0]["name"] == "PROBE"
        assert len(doc["proxy-groups"][0]["proxies"]) == len(proxies)
    else:
        # 空配置不能带空的 select 组，否则真实内核会拒绝（见 mihomo._config 注释）
        assert "proxy-groups" not in doc
        assert doc["rules"] == ["MATCH,DIRECT"]
    return not any(str(p["server"]).startswith("BAD") for p in doc["proxies"])


def main() -> int:
    Mihomo.config_ok = fake_config_ok

    with tempfile.TemporaryDirectory() as tmp:
        mh = Mihomo(Path("unused"), Path(tmp), log=lambda *_: None)

        # 200 条里埋 3 条坏的，位置故意分散在头、中、尾
        proxies = [make_proxy(i) for i in range(200)]
        bad_indexes = {0, 97, 199}
        for i in bad_indexes:
            proxies[i] = make_proxy(i, bad=True)

        good, bad = mh.filter_loadable(proxies)

        problems = []
        if len(bad) != len(bad_indexes):
            problems.append(f"坏条目数 {len(bad)} != {len(bad_indexes)}")
        if {p["name"] for p in bad} != {f"n{i:04d}" for i in bad_indexes}:
            problems.append(f"定位到的坏条目不对: {sorted(p['name'] for p in bad)}")
        if len(good) != len(proxies) - len(bad_indexes):
            problems.append(f"好条目数 {len(good)} 不对")
        if [p["name"] for p in good] != [p["name"] for p in proxies
                                         if p["name"] not in {f"n{i:04d}" for i in bad_indexes}]:
            problems.append("好条目顺序被打乱了")

        # 全坏 / 全好两个极端
        all_bad = [make_proxy(i, bad=True) for i in range(8)]
        good2, bad2 = mh.filter_loadable(all_bad)
        if good2 or len(bad2) != 8:
            problems.append(f"全坏场景不对: good={len(good2)} bad={len(bad2)}")

        good3, bad3 = mh.filter_loadable([make_proxy(i) for i in range(8)])
        if len(good3) != 8 or bad3:
            problems.append(f"全好场景不对: good={len(good3)} bad={len(bad3)}")

        # 空输入
        if mh.filter_loadable([]) != ([], []):
            problems.append("空输入没有返回空结果")

        print(f"二分次数：{CHECKS['n']}（200 条含 3 坏）")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 二分定位通过：坏条目全部找出、好条目全部保留且顺序不变、边界场景正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
