# -*- coding: utf-8 -*-
"""
短线进攻版 · 周度多因子策略

与月度稳健版的区别
    调仓   每 5 个交易日（每周最后一个交易日收盘出信号，次日开盘成交）
    持仓   15 只，更集中
    池子   日均成交额 ≥ 2 亿（要能快进快出）
    因子   全部换成短周期量价：5日反转 / 量能变化 / 突破强度 / 振幅
    成本   换手极高，成本是生死线，必须计入

用法
    python short_term.py            # 跑对照实验并输出最优版本的回测
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import Config, run

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"


# ------------------------------------------------------------------ 调仓日
def week_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每个自然周（ISO 周）最后一个交易日。"""
    per = dates.to_period("W")
    s = pd.Series(dates, index=dates)
    return pd.DatetimeIndex(s.groupby(per).max().values).sort_values()


def biweek_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每两周一次：取隔周的最后交易日。"""
    per = dates.to_period("W")
    s = pd.Series(dates, index=dates)
    g = s.groupby(per).max()
    return pd.DatetimeIndex(g.values[np.arange(len(g)) % 2 == 0]).sort_values()


# ------------------------------------------------------------------ 因子
def st_factors(p: dict) -> dict[str, pd.DataFrame]:
    close = p["close"]
    amt = p["amount"]
    turn = p["turnover"]
    pct = p["pct_chg"].abs()

    ret5 = close / close.shift(5) - 1
    ret20 = close / close.shift(20) - 1
    hi60 = close.rolling(60, min_periods=40).max()

    f = {
        # 短期反转：5 日跌得越多越好
        "rev5": -ret5,
        # 中期反转（震荡市里也很强）
        "rev20": -ret20,
        # 量能放大：近 5 日成交额 / 近 60 日
        "volsurge": amt.rolling(5, min_periods=4).mean()
                    / amt.rolling(60, min_periods=40).mean(),
        # 换手放大
        "turnsurge": turn.rolling(5, min_periods=4).mean()
                     / turn.rolling(60, min_periods=40).mean(),
        # 突破强度：离 60 日高点越近越强（值域 (-1, 0]，越接近 0 越强）
        "brk": close / hi60 - 1,
        # 振幅：近 20 日平均绝对涨跌幅（越大越活跃、越激进）
        "amp": pct.rolling(20, min_periods=15).mean(),
        # 波动（进攻性）
        "vola": close.pct_change(fill_method=None)
                .rolling(20, min_periods=15).std() * np.sqrt(252),
    }
    # 量能类因子做对数，压缩极值
    for k in ("volsurge", "turnsurge"):
        f[k] = np.log(f[k].where(f[k] > 0))
    return f


BASE = dict(
    top_n=15,
    buffer_ratio=1.0,
    min_amt20=2e8,
    min_price=3.0,
    min_list_days=250,
    max_ind_w=0.35,
    max_stock_w=0.12,
    quality_screen=False,
    fwd_days=5,
    commission=0.00025,
    stamp=0.0005,
    slippage=0.0015,      # 短线冲击更大
)


def mk(weights: dict, **kw) -> Config:
    cfg = Config(**{**BASE, **kw})
    cfg.weights = weights
    return cfg


VARIANTS = {
    "A 超跌反弹（反转+缩量+低波）": mk({"rev5": .45, "rev20": .20, "volsurge": -.15,
                                        "amp": -.10, "vola": -.10}),
    "B 超跌反弹（反转+放量）": mk({"rev5": .45, "rev20": .20, "volsurge": .15,
                                  "amp": .10, "vola": .10}),
    "C 强势突破（追涨+放量+高波）": mk({"brk": .45, "volsurge": .25, "amp": .15,
                                        "vola": .15}),
    "D 量价共振（反转+换手放大）": mk({"rev5": .40, "rev20": .20, "turnsurge": .25,
                                       "amp": .15}),
    "E 纯反转": mk({"rev5": .60, "rev20": .40}),
}

# 方向按实测周度 IC 校正后的组合
RIGHT = {"rev20": .35, "rev5": .25, "vola": -.15, "amp": -.15, "volsurge": -.10}


def rf(**kw) -> Config:
    """按正确方向 + 压低换手 + 趋势过滤 的第二轮方案。"""
    import copy
    cfg = mk(copy.deepcopy(RIGHT), **kw)
    return cfg


VARIANTS2 = {
    "F 双周+正确方向": (rf(), biweek_end_dates),
    "G 双周+正确方向+择时MA20": (rf(risk_switch=True, ma_window=20,
                                    off_exposure=0.3), biweek_end_dates),
    "H 周度+正确方向+择时MA20": (rf(risk_switch=True, ma_window=20,
                                    off_exposure=0.3), week_end_dates),
    "I 双周+N25": (rf(top_n=25), biweek_end_dates),
    "J 双周+正确方向+缓冲区1.8": (rf(buffer_ratio=1.8), biweek_end_dates),
    "K 周度+正确方向+强择时(空仓)": (rf(risk_switch=True, ma_window=20,
                                        off_exposure=0.0), week_end_dates),
}


def main():
    rows = []
    cases = {k: (v, week_end_dates) for k, v in VARIANTS.items()}
    cases.update(VARIANTS2)
    for name, (cfg, reb) in cases.items():
        print(f"\n=== {name} ===")
        try:
            r = run(cfg, verbose=False, factor_fn=st_factors, reb_fn=reb)
        except Exception as e:  # noqa: BLE001
            print("  失败:", e)
            continue
        rows.append({
            "方案": name, "年化": r["cagr"], "超额": r["ann_excess"],
            "波动": r["vol"], "夏普": r["sharpe"], "最大回撤": r["mdd"],
            "IR": r["ir"], "年换手": r["ann_turnover"],
        })
        print(f"  年化 {r['cagr']*100:6.1f}%  超额 {r['ann_excess']*100:6.1f}%  "
              f"回撤 {r['mdd']*100:6.1f}%  夏普 {r['sharpe']:.2f}  "
              f"换手 {r['ann_turnover']*100:.0f}%")
    df = pd.DataFrame(rows)
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "short_term_test.csv", index=False, encoding="utf-8-sig")
    md = ["# 短线版对照实验（周度调仓）\n",
          "| 方案 | 年化 | 超额 | 波动 | 夏普 | 最大回撤 | IR | 年换手 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in df.iterrows():
        md.append(f"| {r['方案']} | {r['年化']*100:.1f}% | {r['超额']*100:.1f}% | "
                  f"{r['波动']*100:.1f}% | {r['夏普']:.2f} | {r['最大回撤']*100:.1f}% | "
                  f"{r['IR']:.2f} | {r['年换手']*100:.0f}% |")
    (OUT / "短线版实验.md").write_text("\n".join(md), encoding="utf-8")
    print("\n" + df.to_string(index=False))


if __name__ == "__main__":
    main()
