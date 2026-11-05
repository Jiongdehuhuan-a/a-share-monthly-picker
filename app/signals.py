# -*- coding: utf-8 -*-
"""生成当期选股信号：与回测完全同一套因子与组合规则。"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from . import config, pricing, storage

import backtest as bt

FACTOR_CN = {
    "ep": "EP", "bp": "BP", "momentum": "动量",
    "lowvol": "低波", "turnover": "低换手", "size": "小市值",
    "roe": "ROE", "gm": "毛利率",
}


def _load() -> dict:
    p = {}
    for k in bt.available_panels():
        p[k] = pd.read_parquet(config.PANEL / f"{k}.parquet")
    # 季度面板（roe/gm/yoy_profit）是按建面板当天的交易日历铺值的，
    # 行情每天往后加一天，它就少一天；这里统一按公告日语义前向填充到
    # 行情日历上，避免 generate() 里 .loc[最新信号日] 抛 KeyError。
    base = p["close"]
    for k in bt.OPTIONAL_COLS:
        if k in p:
            p[k] = (p[k].reindex(index=base.index, columns=base.columns)
                        .ffill().astype("float32"))
    p["index"] = pd.read_parquet(config.DATA / "index.parquet")
    p["universe"] = pd.read_parquet(config.DATA / "universe.parquet")
    return p


def _signal_date(p: dict) -> pd.Timestamp:
    """最新可用的信号日：行情与估值都有、且已收盘。

    收盘前不含当天（盘中 bar 不完整）；16:00 之后含当天。
    与 datafeed.panel_status 使用同一套规则，避免两边对不上。
    """
    from .datafeed import _cutoff
    valid = p["close"].index.intersection(p["float_mv"].index)
    valid = valid[valid < _cutoff()]
    if len(valid) == 0:
        raise RuntimeError("估值数据为空，请先更新数据")
    return valid.max()


def make_cfg(s: dict) -> bt.Config:
    return bt.Config(
        top_n=int(s["top_n"]),
        buffer_ratio=float(s["buffer_ratio"]),
        weights=dict(s["weights"]),
        max_stock_w=float(s["max_stock_w"]),
        max_ind_w=float(s["max_ind_w"]),
        min_list_days=int(s["min_list_days"]),
        min_amt20=float(s["min_amt20"]),
        min_price=float(s["min_price"]),
        risk_switch=False,
        quality_screen=bool(s.get("quality_screen", True)),
        quality_tilt=float(s.get("quality_tilt", 0.0)),
    )


def _apply_news(score: pd.Series, elig: pd.Series, name: pd.Series,
                s: dict, cb) -> tuple[dict | None, list]:
    """按新闻情绪调整得分：硬利空剔除、情绪分做有界倾斜。原地修改 score。"""
    from . import news as news_mod

    tilt = float(s.get("news_tilt", 0.15))
    veto = bool(s.get("news_veto", True))
    n_cand = int(max(int(s.get("top_n", 50)) * 4, 200))
    cand = (score[elig].dropna().sort_values(ascending=False)
            .head(n_cand).index.tolist())
    if not cand:
        return None, []
    codes_names = {c: str(name.get(c, "")).strip() for c in cand}
    try:
        cb(0.68, f"抓取 {len(cand)} 只个股新闻…")
        data = news_mod.build(
            codes_names, force=bool(s.get("news_force", False)),
            progress_cb=lambda p, m="": cb(0.68 + 0.22 * p, m))
    except Exception as e:  # noqa: BLE001
        storage.log(f"news failed: {e}")
        return None, []

    vetoed = []
    for c in cand:
        n = data["stocks"].get(c)
        if not n:
            continue
        if veto and n.get("hard"):
            score.loc[c] = np.nan
            vetoed.append({"code": c, "name": codes_names.get(c, ""),
                           "reason": "硬利空"})
            continue
        z = max(-2.0, min(2.0, float(n.get("z", 0.0))))
        score.loc[c] = float(score.loc[c]) + tilt * z
    storage.log(f"news applied: tilt={tilt} vetoed={len(vetoed)}")
    return data, vetoed


def _resolve_capital(s: dict, holdings: dict | None,
                     price: pd.Series) -> tuple[float, str, float, float]:
    """定「账户资金」口径。

    优先用真实总资产（现金 + 持仓按信号日收盘估值），这样「建议股数」和调仓单的
    「目标股数」是同一套算法，不会出现信号页按 20 万、调仓单按 20.24 万打架的情况。
    没有持仓数据（或总资产为 0）时，退回参数设置里的「账户资金」。
    """
    fallback = float(s.get("capital", 1_000_000))
    if not holdings:
        return fallback, "settings", 0.0, 0.0
    try:
        cash = float(holdings.get("cash", 0.0) or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    mv = 0.0
    for c, sh in (holdings.get("positions") or {}).items():
        try:
            px = float(price.get(c, float("nan")))
            if px == px and px > 0:
                mv += float(sh) * px
        except (TypeError, ValueError):
            continue
    total = cash + mv
    if total <= 0:
        return fallback, "settings", cash, mv
    return total, "holdings", cash, mv


def generate(settings: dict | None = None,
             prev_codes: list[str] | None = None,
             progress_cb=None,
             holdings: dict | None = None) -> dict:
    s = settings or config.load_settings()
    cb = progress_cb or (lambda p, m="": None)
    cb(0.05, "读取面板…")
    p = _load()
    d = _signal_date(p)
    cb(0.35, f"信号日 {d.date()}，计算因子…")

    # 截断到信号日，避免盘中不完整 bar 污染滚动窗口
    pt = {k: p[k].loc[:d] for k in bt.available_panels()}
    pt["index"] = p["index"]
    pt["universe"] = p["universe"]
    close = pt["close"]
    adjopen = pt["adjopen"]

    uni = pt["universe"]
    codes = list(close.columns)
    ind = uni.set_index("code")["industry"].reindex(codes)
    name = uni.set_index("code")["name"].reindex(codes)
    is_st = (uni.set_index("code")["name"].astype(str).str.upper()
             .str.contains("ST|退").reindex(codes).fillna(False).astype(bool))

    raw = bt.build_raw_factors(pt)
    amt20 = pt["amount"].rolling(20, min_periods=15).mean()
    listed = close.notna().cumsum()

    cfg = make_cfg(s)
    elig = (close.loc[d].notna() & adjopen.loc[d].notna()
            & (listed.loc[d] >= cfg.min_list_days)
            & (amt20.loc[d] >= cfg.min_amt20)
            & (close.loc[d] >= cfg.min_price)
            & (~is_st)
            & (pt["float_mv"].loc[d] > 0))
    if cfg.quality_screen and "roe" in raw:
        gate = raw["roe"].loc[d] > 0
        if "gm" in raw:
            gate = gate & (raw["gm"].loc[d] > 0)
        elig = elig & gate.reindex(codes).fillna(False).astype(bool)

    cb(0.6, "打分…")
    W = cfg.weights
    tot_s = pd.Series(0.0, index=codes)
    tot_w = pd.Series(0.0, index=codes)
    for k, v in raw.items():
        wk = float(W.get(k, 0.0))
        if wk <= 0:
            continue
        sc = bt._cs_standardize(v.loc[d].where(elig), ind)
        tot_s = tot_s.add((wk * sc).fillna(0.0), fill_value=0.0)
        tot_w = tot_w.add((sc.notna() * wk).astype(float), fill_value=0.0)
    score = (tot_s / tot_w.where(tot_w > 0)).reindex(codes)

    # 筹码集中度倾斜：股东户数降幅排名越高（筹码越集中）加分
    hold_tilt = float(s.get("hold_tilt", 0.0) or 0.0)
    if hold_tilt > 0 and "hold_crowd" in p:
        cw = p["hold_crowd"].loc[d].reindex(codes)
        cw = cw.rank(pct=True) - 0.5
        score = score + hold_tilt * cw.fillna(0.0)

    if float(s.get("quality_tilt", 0.0)) > 0 and "roe" in raw:
        q = 0.5 * bt._cs_standardize(raw["roe"].loc[d].where(elig), ind)
        if "gm" in raw:
            q = q.add(0.5 * bt._cs_standardize(raw["gm"].loc[d].where(elig), ind),
                      fill_value=0.0)
        score = score + float(s["quality_tilt"]) * q.reindex(codes).fillna(0.0)

    # ---------------- 新闻情绪 ----------------
    news_data, vetoed = None, []
    if s.get("news_enabled", True):
        news_data, vetoed = _apply_news(score, elig, name, s, cb)

    cb(0.92, "构建组合…")
    prev = [c for c in (prev_codes or []) if c in set(codes)]
    tw = bt.target_weights(score, elig, ind, prev, cfg)

    # 指数动量择时：中证800 回看 N 日动量为负 → 降仓
    exposure = 1.0
    mom_note = None
    if s.get("mom_timing"):
        try:
            idx = p["index"]
            bm = (idx[idx["index"] == "zz800"].set_index("date")["close"]
                  .reindex(pt["close"].index).ffill())
            win = int(s.get("mom_window", 60))
            mom = float(bm.loc[d] / bm.shift(win).loc[d] - 1.0)
            off = float(s.get("off_exposure", 0.4))
            exposure = 1.0 if mom > 0 else off
            mom_note = {"window": win, "mom": round(mom, 4),
                        "triggered": bool(mom <= 0)}
        except Exception:  # noqa: BLE001
            exposure = 1.0

    price = close.loc[d]
    capital, cap_src, cash_now, mv_now = _resolve_capital(s, holdings, price)
    news_stocks = (news_data or {}).get("stocks", {})
    # 参考买卖价：个股波动率 + 综合分分位对应的历史预期涨幅
    sigma_d = pricing.sigma_at(pt["close"], d)
    rank_pct = score.rank(pct=True)
    picks = []
    for c, w in tw.sort_values(ascending=False).items():
        px = float(price.get(c, np.nan))
        tgt = capital * exposure * float(w)
        shares = int(round(tgt / px / 100) * 100) if px and px > 0 else 0
        nw = news_stocks.get(c) or {}
        try:
            rp = float(rank_pct.get(c, 0.9))
        except (TypeError, ValueError):
            rp = 0.9
        band = pricing.band(px, sigma_d.get(c, np.nan),
                            pricing.expected_return(rp))
        picks.append({
            "code": c,
            "name": str(name.get(c, "")).strip(),
            "industry": str(ind.get(c, "")),
            "weight": round(float(w), 4),
            "score": round(float(score.get(c, np.nan)), 4),
            "price": round(px, 2) if px == px else None,
            "shares": shares,
            "amount": round(shares * px, 0) if px == px else 0,
            "pricing": band,
            "news": ({"score": nw.get("score", 0.0), "z": nw.get("z", 0.0),
                      "label": ("利好" if nw.get("z", 0) > 0.6 else
                                "利空" if nw.get("z", 0) < -0.6 else "中性"),
                      "n": nw.get("n", 0), "pos": nw.get("pos", 0),
                      "neg": nw.get("neg", 0),
                      "items": nw.get("items", [])[:3]}
                     if nw else None),
            "factors": {k: (round(float(v.loc[d].get(c)), 4)
                            if v.loc[d].get(c) == v.loc[d].get(c) else None)
                        for k, v in raw.items()},
        })

    ind_cnt = pd.Series([x["industry"] for x in picks]).value_counts().to_dict()
    sig = {
        "date": str(d.date()),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_eligible": int(elig.sum()),
        "n_picks": len(picks),
        "capital": round(float(capital), 0),
        "capital_source": cap_src,          # holdings=真实总资产 / settings=参数设置里的值
        "exposure": round(float(exposure), 3),
        "mom_note": mom_note,
        "hold_tilt": round(hold_tilt, 3),
        "cash": round(float(cash_now), 0),
        "market_value": round(float(mv_now), 0),
        "settings": {
            "top_n": cfg.top_n, "max_ind_w": cfg.max_ind_w,
            "min_amt20": cfg.min_amt20,
            "quality_screen": cfg.quality_screen,
            "quality_tilt": cfg.quality_tilt,
            "news_enabled": bool(s.get("news_enabled", True)),
            "news_tilt": float(s.get("news_tilt", 0.15)),
            "news_veto": bool(s.get("news_veto", True)),
            "weights": cfg.weights,
        },
        "market_news": (news_data or {}).get("market"),
        "news_vetoed": vetoed,
        "industry_count": ind_cnt,
        "picks": picks,
        "weights": {x["code"]: x["weight"] for x in picks},
    }
    storage.save_signal(sig)
    cb(1.0, "完成")
    storage.log(f"signal {sig['date']} picks={len(picks)} eligible={sig['n_eligible']}")
    return sig
