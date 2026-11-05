# -*- coding: utf-8 -*-
"""冒烟测试：用合成数据把「因子 → 选股 → 撮合 → 绩效」整条链路跑一遍。

    python tests/smoke_backtest.py

不依赖真实行情、不联网、不写仓库里的 data/（全部在临时目录里完成），
几十秒内跑完。CI 里每次 push 都跑这个，保证重构不会把回测跑挂。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt          # noqa: E402
from fake_data import build    # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aqu_smoke_"))
    meta = build(tmp / "data")
    print(f"合成数据: {meta['n_stocks']} 只 × {meta['n_days']} 天  "
          f"{meta['start']} ~ {meta['end']}")

    # 把回测模块指向临时目录，绝不碰仓库里真实的 data/
    bt.ROOT = tmp
    bt.PANEL = tmp / "data" / "panel"
    bt.OUT = tmp / "output"

    panels = bt.load_panels()
    assert "close" in panels and panels["close"].shape[1] == meta["n_stocks"]

    cfg = bt.Config(
        start=meta["start"], end=meta["end"],
        top_n=10, buffer_ratio=1.5,
        min_amt20=1e6, min_price=1.0, min_list_days=20,
    )
    res = bt.run(cfg, verbose=False)

    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    check("净值序列长度", len(res["nav"]) > 60, f"{len(res['nav'])} 天")
    check("年化收益是有限数", np.isfinite(res["cagr"]), f"{res['cagr']:.4f}")
    check("最大回撤 ≤ 0", res["mdd"] <= 0, f"{res['mdd']:.4f}")
    check("夏普是有限数", np.isfinite(res["sharpe"]), f"{res['sharpe']:.2f}")
    check("有调仓记录", len(res["turnover_df"]) > 3, f"{len(res['turnover_df'])} 次")
    check("期末组合非空", len(res["last_portfolio"]["weights"]) > 0,
          f"{len(res['last_portfolio']['weights'])} 只")
    check("IC 有记录", len(res["ic_df"]) > 3, f"{len(res['ic_df'])} 期")
    check("净值无 NaN", bool(res["nav"].notna().all()))

    # 单票权重不超上限、行业不超上限
    w = pd.Series(res["last_portfolio"]["weights"])
    check("单票权重 ≤ 10%", bool((w <= 0.1001).all()), f"max={w.max():.3f}")

    failed = [c for c in checks if not c[1]]
    print()
    if failed:
        print(f"失败 {len(failed)} 项")
        return 1
    print(f"全部 {len(checks)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
