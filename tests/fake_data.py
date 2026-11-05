# -*- coding: utf-8 -*-
"""生成一套小规模合成面板，供 CI / 冒烟测试用。

真实数据 1.3 GB 且依赖东财接口，CI 里不可能拉。这个模块造一套
结构完全一致、但只有几十只股票几百天的假数据，能跑通整条回测链路。

    from fake_data import build
    build(Path("data"))     # 写 data/panel/*.parquet + index + universe
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

INDUSTRIES = ["通用设备", "汽车零部件", "化学制品", "电网设备",
              "医疗器械", "光学光电子", "软件开发", "饮料乳品"]


def build(out_dir: Path, n_stocks: int = 90, n_days: int = 340,
          seed: int = 7) -> dict:
    out_dir = Path(out_dir)
    panel = out_dir / "panel"
    panel.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days)
    codes = [f"6{i:05d}" for i in range(n_stocks)]

    # ---- 价格：几何随机游走 ----
    drift = rng.normal(0.0004, 0.0006, n_stocks)
    vol = rng.uniform(0.012, 0.030, n_stocks)
    shocks = rng.normal(0, 1, (n_days, n_stocks)) * vol + drift
    close = 10.0 * np.exp(np.cumsum(shocks, axis=0))
    close_df = pd.DataFrame(close, index=dates, columns=codes).astype("float32")

    # ---- 开盘、涨跌幅、换手、成交额 ----
    gap = rng.normal(0, 0.006, (n_days, n_stocks))
    prev = close_df.shift(1).bfill()
    adjopen = (prev * (1 + gap)).astype("float32")
    pct_chg = (close_df / prev - 1) * 100
    pct_chg = pct_chg.astype("float32")
    turnover = pd.DataFrame(
        np.abs(rng.normal(1.8, 0.8, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    amount = (close_df * turnover * rng.uniform(6e7, 3e8, n_stocks)).astype("float32")

    # ---- 估值 ----
    pe = pd.DataFrame(
        np.abs(rng.normal(22, 10, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    pb = pd.DataFrame(
        np.abs(rng.normal(2.4, 1.2, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    fmv = pd.DataFrame(
        np.abs(rng.normal(8e9, 4e9, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")

    for name, df in [("close", close_df), ("adjopen", adjopen),
                     ("turnover", turnover), ("amount", amount),
                     ("pct_chg", pct_chg), ("pe_ttm", pe), ("pb", pb),
                     ("float_mv", fmv)]:
        df.index.name = "date"
        df.to_parquet(panel / f"{name}.parquet")

    # ---- 季度因子（按公告日语义铺在交易日上）----
    roe = pd.DataFrame(
        np.abs(rng.normal(10, 6, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    gm = pd.DataFrame(
        np.abs(rng.normal(28, 12, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    yoy = pd.DataFrame(
        rng.normal(12, 25, (n_days, n_stocks)), index=dates, columns=codes
    ).astype("float32")
    # 盈利质量：经营现金流 / 净利润，正常公司多在 0.5~1.5，留点离群值
    ocf_ni = pd.DataFrame(
        np.abs(rng.normal(1.05, 0.7, (n_days, n_stocks))), index=dates, columns=codes
    ).astype("float32")
    for name, df in [("roe", roe), ("gm", gm), ("yoy_profit", yoy), ("ocf_ni", ocf_ni)]:
        df.index.name = "date"
        df.to_parquet(panel / f"{name}.parquet")

    # ---- 指数 ----
    rows = []
    for name, base in [("zz800", 4000), ("hs300", 3800), ("zz500", 6000),
                       ("zz1000", 6500), ("cyb", 2500)]:
        walk = base * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n_days)))
        rows.append(pd.DataFrame({
            "date": dates, "index": name, "close": walk.astype("float32"),
            "open": walk.astype("float32"), "high": walk.astype("float32"),
            "low": walk.astype("float32"), "volume": 1e9, "amount": 1e11,
        }))
    pd.concat(rows, ignore_index=True).to_parquet(out_dir / "index.parquet")

    # ---- 股票池 ----
    uni = pd.DataFrame({
        "code": codes,
        "market": ["sh"] * n_stocks,
        "name": [f"测试{i:02d}" for i in range(n_stocks)],
        "total_mv": fmv.iloc[-1].values * 1.6,
        "float_mv": fmv.iloc[-1].values,
        "list_date": pd.Timestamp("2015-01-05"),
        "industry": [INDUSTRIES[i % len(INDUSTRIES)] for i in range(n_stocks)],
    })
    uni.to_parquet(out_dir / "universe.parquet", index=False)

    return {"start": str(dates[0].date()), "end": str(dates[-1].date()),
            "n_days": int(n_days), "n_stocks": int(n_stocks)}
