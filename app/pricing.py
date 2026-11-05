# -*- coding: utf-8 -*-
"""参考买入 / 卖出价。

三个输入：
  · 现价（信号日收盘，前复权）
  · 个股日波动 σ（60 日）
  · 综合分所处的截面分位 → 未来 21 日预期涨幅（历史分箱，见 src/price_model.py）

产出两组价位：
  买入：挂单价（现价下方 0.5σ，等回调）、可追上限（现价上方 90% 分位跳空）
  卖出：挂单价（现价上方 0.5σ）、最低接受价（现价下方 10% 分位跳空）
另外给 21 日目标价与止损参考。

参数全部来自 data/price_model.json（用历史数据标定），缺失时退回默认值。
"""
from __future__ import annotations

import json

import pandas as pd

from . import config

DEFAULTS = {
    "vol_window": 60,
    "buy_k": 0.5,           # 挂单价：现价 − 0.5σ
    "sell_k": 0.5,          # 挂单价：现价 + 0.5σ
    "chase_gap": 0.010,     # 可追上限：现价 +1.0%（跳空 p90）
    "floor_gap": -0.012,    # 最低接受：现价 −1.2%（跳空 p10）
    "stop_k": 1.5,          # 止损参考：现价 − 1.5σ
    "tp_k": 2.0,            # 止盈：至少 2σ 或 目标涨幅
    "horizon_days": 21,
    "sigma_median": 0.02,
    "bin_mean_ret": [],
}

_CACHE: dict = {"mtime": None, "m": None}


def model() -> dict:
    f = config.DATA / "price_model.json"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return dict(DEFAULTS)
    if _CACHE["mtime"] != mt:
        m = dict(DEFAULTS)
        try:
            m.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
        _CACHE.update(mtime=mt, m=m)
    return _CACHE["m"]


def sigma_at(close: pd.DataFrame, d) -> pd.Series:
    """信号日的个股日波动率（60 日滚动标准差）。"""
    win = int(model().get("vol_window", 60))
    ret = close.pct_change(fill_method=None)
    return ret.rolling(win, min_periods=max(20, win // 2)).std().loc[d]


def expected_return(rank_pct: float) -> float:
    """按综合分截面分位取历史同期平均涨幅。"""
    bins = model().get("bin_mean_ret") or []
    if not bins:
        return 0.0
    try:
        p = float(rank_pct)
    except (TypeError, ValueError):
        return 0.0
    if p != p:
        return 0.0
    i = int(min(len(bins) - 1, max(0, p * len(bins))))
    return float(bins[i])


def _r(x):
    return None if x is None or x != x else round(float(x), 2)


def band(price, sig, exp_ret: float = 0.0) -> dict:
    m = model()
    try:
        px = float(price)
    except (TypeError, ValueError):
        return {}
    if not px or px <= 0 or px != px:
        return {}
    try:
        s = float(sig)
    except (TypeError, ValueError):
        s = float("nan")
    if not (s > 0) or s != s:
        s = float(m.get("sigma_median", 0.02))

    kb = float(m.get("buy_k", 0.5))
    ks = float(m.get("sell_k", 0.5))
    chase = float(m.get("chase_gap", 0.01))
    floor = float(m.get("floor_gap", -0.012))
    stop_k = float(m.get("stop_k", 1.5))
    er = float(exp_ret or 0.0)

    # 止盈：至少给 2 个日波动，且不低于评分给出的 21 日目标涨幅
    tp_ret = max(2.0 * s, er)
    return {
        "take_profit": _r(px * (1 + tp_ret)),
        "buy_limit": _r(px * (1 - kb * s)),
        "buy_cap": _r(px * (1 + chase)),
        "sell_limit": _r(px * (1 + ks * s)),
        "sell_floor": _r(px * (1 + floor)),
        "target": _r(px * (1 + er)) if er else None,
        "stop": _r(px * (1 - stop_k * s)),
        "sigma": round(s, 4),
        "exp_ret": round(er, 4),
    }
