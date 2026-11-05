# -*- coding: utf-8 -*-
"""标定「参考买卖价」：挂单宽度 + 综合分对应的预期涨幅。

输出 data/price_model.json，供 app/pricing.py 使用。

    python src/price_model.py

口径说明（重要）：
- 只有日线，没有分时。所以“限价单能不能成交”用「次日最低可得价」
  min(次日开盘, 次日收盘) 作为代理：只要这个价跌到过限价下方，就认为能成交。
  这是偏保守的下界（真实盘中低点通常更低），比只看收盘价合理。
- 挂单宽度 k 取「成交率 ≈ 65%」的那一档：既不是每单都抢开盘，
  也不会挂太深导致一个月都成交不了。
- 综合分 → 预期涨幅用分位数分箱（20 档），因为策略只取最前面那一小撮，
  十分位太粗，最后一档内部差别很大。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import backtest as bt  # noqa: E402

OUT = ROOT / "data" / "price_model.json"
VOL_WIN = 60
KS = np.round(np.arange(0.1, 2.01, 0.05), 2)
TARGET_FILL = 0.65
N_BINS = 20


def calibrate_bands(p: dict) -> dict:
    """算两样东西：

    1. 隔夜跳空分布（次日开盘 / 今收 − 1）——实盘是在开盘附近成交的，
       所以“可追价”应该用跳空分位数定，而不是拍脑袋。
    2. 在现价下方/上方 0.5σ 挂限价的历史成交率（最低可得价口径）。
    """
    close, op = p["close"], p["adjopen"]
    ret = close.pct_change(fill_method=None)
    sig = ret.rolling(VOL_WIN, min_periods=40).std()

    sample = close.index[::5]
    c = close.loc[sample]
    s = sig.loc[sample]
    no = op.shift(-1).loc[sample]
    nc = close.shift(-1).loc[sample]
    mask = (c > 0) & (no > 0) & (nc > 0) & s.notna()
    r_open = (no / c).where(mask)
    r_close = (nc / c).where(mask)
    r_low = np.minimum(r_open, r_close)
    r_high = np.maximum(r_open, r_close)
    sv = s.where(mask)
    n_obs = int(mask.sum().sum())

    gap = (r_open - 1.0).values.ravel()
    gap = gap[~np.isnan(gap)]
    q = {f"p{int(x*100)}": float(np.quantile(gap, x)) for x in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)}

    out = {"n_obs": n_obs, "gap": q,
           "sigma_median": float(np.nanmedian(sv.values)),
           "fill_curve": []}

    for k in np.round(np.arange(0.2, 1.6, 0.1), 2):
        lim = 1.0 - k * sv
        buy_fill = float((r_low <= lim).sum().sum() / n_obs)
        lims = 1.0 + k * sv
        sell_fill = float((r_high >= lims).sum().sum() / n_obs)
        # 成交时的成交价相对次日开盘的差异（负=买得更便宜）
        disc = float(np.nanmean(((lim / r_open) - 1.0).where(r_low <= lim))) * 1e4
        prem = float(np.nanmean(((lims / r_open) - 1.0).where(r_high >= lims))) * 1e4
        out["fill_curve"].append({"k": float(k), "buy_fill": buy_fill,
                                  "sell_fill": sell_fill,
                                  "buy_vs_open_bp": disc, "sell_vs_open_bp": prem})
    return out


def score_bins(p: dict) -> dict:
    """按月频调仓日：综合分分位数分箱 → 未来 21 日平均涨幅。"""
    dates = p["close"].index
    valued = dates.intersection(p["float_mv"].index)
    reb = bt.month_end_dates(valued)
    reb = reb[reb <= dates[-25]]

    raw = bt.build_raw_factors(p)
    W = bt.default_weights()
    uni = p["universe"]
    ind = uni.set_index("code")["industry"].reindex(p["close"].columns)
    amt20 = p["amount"].rolling(20, min_periods=15).mean()
    listed = p["close"].notna().cumsum()
    fwd = p["close"].shift(-21) / p["close"] - 1.0
    roe = p["roe"].reindex(index=dates).ffill() if "roe" in p else None
    gm = p["gm"].reindex(index=dates).ffill() if "gm" in p else None

    buckets: list[list[float]] = [[] for _ in range(N_BINS)]
    per_year: dict[int, list[list[float]]] = {}
    for d in reb:
        elig = (p["close"].loc[d].notna() & p["adjopen"].loc[d].notna()
                & (listed.loc[d] >= 250) & (amt20.loc[d] >= 5e7)
                & (p["close"].loc[d] >= 2.0) & (p["float_mv"].loc[d] > 0))
        if roe is not None:
            g = (roe.loc[d] > 0)
            if gm is not None:
                g = g & (gm.loc[d] > 0)
            elig = elig & g.fillna(False)
        tot_s = pd.Series(0.0, index=p["close"].columns)
        tot_w = pd.Series(0.0, index=p["close"].columns)
        for k, v in raw.items():
            wk = float(W.get(k, 0.0))
            if wk <= 0:
                continue
            sc = bt._cs_standardize(v.loc[d].where(elig), ind)
            ok = sc.notna()
            tot_s = tot_s.add((wk * sc).fillna(0.0), fill_value=0.0)
            tot_w = tot_w.add((ok * wk).astype(float), fill_value=0.0)
        score = (tot_s / tot_w.where(tot_w > 0)).where(elig)
        fr = fwd.loc[d]
        ok = score.notna() & fr.notna()
        if ok.sum() < 500:
            continue
        pct = score[ok].rank(pct=True)
        b = np.minimum((pct * N_BINS).astype(int), N_BINS - 1)
        for i in range(N_BINS):
            vals = fr[ok][b == i]
            if len(vals):
                buckets[i].extend(vals.tolist())
                per_year.setdefault(d.year, [[] for _ in range(N_BINS)])[i].extend(
                    vals.tolist())

    table = [float(np.mean(x)) if x else 0.0 for x in buckets]
    years = {str(y): [float(np.mean(x)) if x else 0.0 for x in rows]
             for y, rows in sorted(per_year.items())}
    return {"bin_mean_ret": table, "by_year": years,
            "count": [len(x) for x in buckets]}


def main():
    p = bt.load_panels()
    print("[1/2] 标定挂单区间 …")
    st = calibrate_bands(p)
    g = st["gap"]
    print(f"     样本 {st['n_obs']:,} 个股票日，日波动中位数 "
          f"{st['sigma_median']*100:.2f}%")
    print(f"     隔夜跳空 中位 {g['p50']*100:+.2f}%  "
          f"p10 {g['p10']*100:+.2f}%  p90 {g['p90']*100:+.2f}%  "
          f"p95 {g['p95']*100:+.2f}%")
    for row in st["fill_curve"]:
        if abs(row["k"] - 0.5) < 1e-9:
            print(f"     挂 ±0.50σ：买单成交率 {row['buy_fill']*100:.0f}%，"
                  f"卖单成交率 {row['sell_fill']*100:.0f}%")

    print("[2/2] 综合分分箱 → 21 日涨幅 …")
    dec = score_bins(p)
    for i, v in enumerate(dec["bin_mean_ret"], 1):
        print(f"      第{i:2d}/20 档 {v*100:+.2f}%")

    buy_k, sell_k = 0.5, 0.5
    chase = max(0.008, min(0.03, g["p90"]))
    floor = min(-0.008, max(-0.03, g["p10"]))
    model = {
        "created": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "vol_window": VOL_WIN,
        "buy_k": buy_k, "sell_k": sell_k,
        "chase_gap": chase,      # 买单可追上限 = 现价 ×(1+chase_gap)
        "floor_gap": floor,      # 卖单最低接受 = 现价 ×(1+floor_gap)
        "stop_k": 1.5,           # 止损参考 = 现价 ×(1−1.5σ)
        "horizon_days": 21,
        "gap": g, "sigma_median": st["sigma_median"],
        "fill_curve": st["fill_curve"], "n_obs": st["n_obs"],
        "n_bins": N_BINS,
        "bin_mean_ret": dec["bin_mean_ret"],
        "bin_by_year": dec["by_year"],
        "bin_count": dec["count"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(model, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n已写出 {OUT}")


if __name__ == "__main__":
    main()
