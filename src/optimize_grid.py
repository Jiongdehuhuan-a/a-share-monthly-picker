# -*- coding: utf-8 -*-
"""策略参数网格实验（可复现）。

    python src/optimize_grid.py            # 全区间 + 样本内外切分，约 4~6 分钟

结果与结论见 output/策略优化.md。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt  # noqa: E402

PANELS = bt.load_panels()
bt.load_panels = lambda: PANELS          # 面板只读一次，后面复用
OUT = bt.OUT
OUT.mkdir(parents=True, exist_ok=True)


def factors(p: dict) -> dict:
    close, turn = p["close"], p["turnover"]
    ret = close.pct_change(fill_method=None)
    pe, pb, fmv = p["pe_ttm"], p["pb"], p["float_mv"]
    return {
        "ep": (1.0 / pe).where(pe > 0),
        "bp": (1.0 / pb).where(pb > 0),
        "momentum": close.shift(21) / close.shift(126) - 1.0,
        "mom12": close.shift(21) / close.shift(252) - 1.0,
        "lowvol": -ret.rolling(60, min_periods=40).std() * np.sqrt(252),
        "turnover": -turn.rolling(20, min_periods=15).mean(),
        "size": -np.log(fmv.where(fmv > 0)),
        "reversal": -(close / close.shift(20) - 1.0),
    }


RAW = factors(PANELS)
FAC = lambda _p: RAW  # noqa: E731

SIX = ["ep", "bp", "momentum", "lowvol", "turnover", "size"]


def w(rev: float, mom: str = "momentum") -> dict:
    d = {k: 1.0 for k in SIX}
    if mom != "momentum":
        d.pop("momentum")
        d[mom] = 1.0
    if rev:
        d["reversal"] = rev
    return d


CASES = [
    ("旧口径(12-1,无反转,等权)", dict(weights=w(0.0, "mom12"))),
    ("旧口径+反转0.3", dict(weights=w(0.3, "mom12"))),
    ("新口径(6-1,无反转)", dict(weights=w(0.0))),
    ("新口径+反转0.3(默认)", dict(weights=w(0.3))),
    ("新口径+反转0.5", dict(weights=w(0.5))),
    ("新口径+反转1.0", dict(weights=w(1.0))),
    ("新口径+反转0.3,N30", dict(weights=w(0.3), top_n=30)),
    ("新口径+反转0.3,N10", dict(weights=w(0.3), top_n=10)),
    ("新口径+反转0.3+质量倾斜0.1", dict(weights=w(0.3), quality_tilt=0.1)),
    ("新口径+反转0.3+均线择时", dict(weights=w(0.3), risk_switch=True)),
]

rows = []
for name, kw in CASES:
    t0 = time.time()
    r = bt.run(bt.Config(**kw), verbose=False, factor_fn=FAC)
    row = {"name": name, **{k: r[k] for k in
           ("cagr", "ann_excess", "sharpe", "mdd", "calmar", "ir", "ann_turnover")},
           "sec": round(time.time() - t0, 1)}
    for tag, (s, e) in {"IS": ("2019-01-01", "2023-12-31"),
                        "OOS": ("2024-01-01", "2026-11-03")}.items():
        rs = bt.run(bt.Config(start=s, end=e, **kw), verbose=False, factor_fn=FAC)
        row[f"{tag}_cagr"], row[f"{tag}_sharpe"] = rs["cagr"], rs["sharpe"]
    rows.append(row)
    print(f"  {name:26s} cagr={row['cagr']:.4f} sharpe={row['sharpe']:.2f} "
          f"mdd={row['mdd']:.3f} to={row['ann_turnover']:.2f} | "
          f"IS {row['IS_cagr']:.3f}/{row['IS_sharpe']:.2f} "
          f"OOS {row['OOS_cagr']:.3f}/{row['OOS_sharpe']:.2f}", flush=True)

df = pd.DataFrame(rows)
df.to_csv(OUT / "optimize_grid.csv", index=False, encoding="utf-8-sig")
print("\n" + df.to_string(index=False))
print("\n已写出:", OUT / "optimize_grid.csv")
