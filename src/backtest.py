# -*- coding: utf-8 -*-
"""
A 股月度多因子选股策略 —— 回测引擎

策略骨架
--------
    股票池 : 全 A 股（剔除 ST / 上市不足 1 年 / 流动性不足）
    因子   : 价值(EP,BP) 动量(12-1) 低波(60日) 低换手(20日) 小市值
    打分   : 截面 rank -> 行业中性 -> 加权合成（缺失因子自动降权）
    组合   : 选前 N 只等权，行业上限 25%，单票上限 5%，缓冲区降换手
    风控   : 中证 800 位于 200 日均线下方时仓位降到 40%
    执行   : 月末收盘出信号，次日开盘成交，含佣金/印花税/滑点，涨跌停不可成交

用法
----
    python backtest.py
    python backtest.py --no-risk-switch --top-n 30
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel"
OUT = ROOT / "output"

PANEL_COLS = ["close", "adjopen", "turnover", "amount", "pct_chg",
              "pe_ttm", "pb", "float_mv"]
# 财务面板（需要先跑 build_fundamentals.py；缺失时自动跳过对应因子）
OPTIONAL_COLS = ["roe", "gm", "yoy_profit", "ocf_ni"]

FACTORS = ["ep", "bp", "momentum", "lowvol", "turnover", "size",
           "reversal", "ocf_ni", "roe", "gm"]
BASE_FACTORS = ["ep", "bp", "momentum", "lowvol", "turnover", "size"]
N_FACTOR = len(BASE_FACTORS)


def base_weights() -> dict:
    return {k: 1 / N_FACTOR for k in BASE_FACTORS}


def default_weights() -> dict:
    """调优后的默认权重：7 个主因子等权（含盈利质量），短期反转给 0.3 的相对权重。

    0.3 是网格里夏普最高的一档（0.77 vs 不加反转 0.74 vs 反转等权 0.70），
    见 output/策略优化.md。
    """
    w = {k: 1.0 for k in BASE_FACTORS}
    w["ocf_ni"] = 1.0          # 盈利质量：经营现金流/净利润，等权
    w["reversal"] = 0.3
    tot = sum(w.values())
    return {k: v / tot for k, v in w.items()}


def equal_weights() -> dict:
    """八因子等权（含质量因子，仅用于对照实验）。"""
    return {k: 1 / len(FACTORS) for k in FACTORS}


def available_panels() -> list[str]:
    return PANEL_COLS + [c for c in OPTIONAL_COLS if (PANEL / f"{c}.parquet").exists()]


@dataclass
class Config:
    start: str = "2019-01-01"
    end: str = "2026-12-31"
    top_n: int = 50
    buffer_ratio: float = 1.5
    weights: dict = field(default_factory=default_weights)
    max_stock_w: float = 0.05
    max_ind_w: float = 0.25
    min_list_days: int = 250
    min_amt20: float = 5e7
    min_price: float = 2.0
    risk_switch: bool = False
    quality_screen: bool = True       # 用 ROE>0 且毛利率>0 做质量门槛（剔除亏损股）
    quality_tilt: float = 0.0         # 质量倾斜强度（不改变六因子的相对权重）
    fwd_days: int = 21                # 计算 IC 的前瞻天数（周度策略用 5）
    ma_window: int = 200
    off_exposure: float = 0.40
    rf: float = 0.015
    commission: float = 0.00025
    stamp: float = 0.0005
    slippage: float = 0.001

    def label(self) -> str:
        return f"N{self.top_n}_" + ("RS" if self.risk_switch else "noRS")


def load_panels() -> dict:
    out = {}
    for k in PANEL_COLS:
        f = PANEL / f"{k}.parquet"
        if not f.exists():
            raise FileNotFoundError(f"缺少面板 {f}，请先运行 consolidate.py")
        out[k] = pd.read_parquet(f)
    idx = out["close"].index
    cols = out["close"].columns
    for k in OPTIONAL_COLS:
        f = PANEL / f"{k}.parquet"
        if f.exists():
            # 季度面板只覆盖到建面板那天，往后新增的交易日要前向填充，
            # 否则最新一天的 roe/gm 全是 NaN，质量门槛会悄悄失效。
            out[k] = (pd.read_parquet(f).reindex(index=idx, columns=cols)
                      .ffill().astype("float32"))
    out["index"] = pd.read_parquet(ROOT / "data" / "index.parquet")
    out["universe"] = pd.read_parquet(ROOT / "data" / "universe.parquet")
    return out


def month_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(dates, index=dates)
    return pd.DatetimeIndex(s.groupby([dates.year, dates.month]).max().values)


def build_raw_factors(p: dict) -> dict[str, pd.DataFrame]:
    close, turn = p["close"], p["turnover"]
    ret = close.pct_change(fill_method=None)
    pe, pb = p["pe_ttm"], p["pb"]
    f = {
        "ep": (1.0 / pe).where(pe > 0),
        "bp": (1.0 / pb).where(pb > 0),
        # 动量改成 6-1：skip 最近 21 日，取 126 日涨幅。
        # 原 252 日版在 2019-2026 全样本 RankIC 为负（-0.015），
        # 换成 6-1 后组合年化 +1.5pp、夏普 +0.06（见 output/策略优化.md）。
        "momentum": close.shift(21) / close.shift(126) - 1.0,
        "lowvol": -ret.rolling(60, min_periods=40).std() * np.sqrt(252),
        "turnover": -turn.rolling(20, min_periods=15).mean(),
        "size": -np.log(p["float_mv"].where(p["float_mv"] > 0)),
        # 短期反转：20 日涨幅取负。A 股最强的单因子（RankIC 0.064，t=3.9）
        "reversal": -(close / close.shift(20) - 1.0),
    }
    # 质量因子：ROE（年化）与销售毛利率，越高越好
    if "roe" in p:
        f["roe"] = p["roe"].where(p["roe"].abs() < 200)
    if "gm" in p:
        f["gm"] = p["gm"].where((p["gm"] >= -50) & (p["gm"] <= 100))
    # 盈利质量（应计因子）：经营现金流 / 净利润，越高说明利润越“真”
    if "ocf_ni" in p:
        v = p["ocf_ni"].where((p["ocf_ni"] >= -5) & (p["ocf_ni"] <= 5))   # 极端值截断
        try:
            ind = (p["universe"].set_index("code")["industry"]
                   .reindex(p["close"].columns).astype(str))
            fin = ind.str.contains("银行|保险|证券|多元金融|信托", regex=True)
            if fin.any():
                v.loc[:, fin[fin].index] = np.nan   # 金融股口径失真，不参与
        except Exception:  # noqa: BLE001
            pass
        f["ocf_ni"] = v
    return f


def _rank_corr(a: pd.Series, b: pd.Series) -> float:
    """Spearman 秩相关（不依赖 scipy）。"""
    if len(a) < 10:
        return np.nan
    return float(a.rank().corr(b.rank()))


def _cs_standardize(s: pd.Series, ind: pd.Series) -> pd.Series:
    """截面 rank -> [-0.5,0.5] -> 行业去均值 -> 除以截面标准差。"""
    r = s.rank(pct=True) - 0.5
    r = r - r.groupby(ind).transform("mean")
    sd = r.std()
    return r / sd if sd and sd > 0 else r


def _cap_weights(w: pd.Series, cap: float) -> pd.Series:
    w = w.copy()
    for _ in range(30):
        over = w[w > cap + 1e-12]
        if over.empty:
            break
        excess = float((over - cap).sum())
        w.loc[over.index] = cap
        room = w[w < cap - 1e-12]
        if room.empty or room.sum() <= 0:
            break
        w.loc[room.index] += excess * room / room.sum()
    return w


def target_weights(sc: pd.Series, elig: pd.Series, ind: pd.Series,
                   prev: list[str], cfg: Config) -> pd.Series:
    """打分选股 + 缓冲区降换手 + 行业/单票权重上限。

    缓冲区（buffer_ratio）：老持仓只要还排在 top_n ~ top_n*buffer 名次内，
    就顶掉排名最靠后的“新进者”继续持有，而不是被一刀切换掉。
    旧实现把这层逻辑写成了“取并集再截断 top_n”，等价于永远只取前 top_n，
    buffer_ratio 调成 1.2 或 2.0 结果一模一样（换手率因此虚高）。
    """
    cand = sc[elig].dropna().sort_values(ascending=False)
    if cand.empty:
        return pd.Series(dtype=float)
    n = int(cfg.top_n)
    held = set(prev)
    picked = list(cand.index[:n])
    if held:
        n_buf = max(n + 1, int(round(n * max(1.0, cfg.buffer_ratio))))
        keepers = [c for c in cand.index[n:n_buf] if c in held]
        for c in keepers:
            slot = None
            for j in range(len(picked) - 1, -1, -1):
                if picked[j] not in held:
                    slot = j
                    break
            if slot is None:
                break
            picked[slot] = c
    picked = sorted(set(picked), key=lambda c: cand[c], reverse=True)
    w = pd.Series(1.0 / len(picked), index=picked)

    for _ in range(3):
        iw = w.groupby(ind.reindex(w.index)).sum()
        over = iw[iw > cfg.max_ind_w]
        if over.empty:
            break
        for k in over.index:
            members = [c for c in w.index if ind.get(c) == k]
            w.loc[members] *= cfg.max_ind_w / over[k]
        w = w / w.sum()

    if (w > cfg.max_stock_w).any():
        w = _cap_weights(w, cfg.max_stock_w)
    return w / w.sum()


def run(cfg: Config, verbose: bool = True, factor_fn=None, reb_fn=None) -> dict:
    """factor_fn(p) -> dict[str, DataFrame]；reb_fn(dates) -> 调仓日。"""
    p = load_panels()
    all_dates = p["close"].index
    # 只保留：估值面板也有的日期（数据完整），且剔除当日（盘中为不完整 bar）
    today = pd.Timestamp.now().normalize()
    valid = all_dates.intersection(p["float_mv"].index)
    valid = valid[valid < today]
    all_dates = valid
    dates = all_dates[(all_dates >= pd.Timestamp(cfg.start)) & (all_dates <= pd.Timestamp(cfg.end))]
    close = p["close"].reindex(all_dates)
    adjopen = p["adjopen"].reindex(all_dates)
    close = close.loc[dates]
    adjopen = adjopen.loc[dates]
    codes = list(close.columns)
    col_pos = {c: i for i, c in enumerate(codes)}

    uni = p["universe"]
    ind = uni.set_index("code")["industry"].reindex(codes)
    is_st = (uni.set_index("code")["name"].astype(str).str.upper()
             .str.contains("ST|退").reindex(codes).fillna(False).astype(bool))

    raw = (factor_fn or build_raw_factors)(p)
    amt20 = p["amount"].rolling(20, min_periods=15).mean()
    listed = p["close"].notna().cumsum()

    idx = p["index"]
    bm = (idx[idx["index"] == "zz800"].set_index("date")["close"]
          .reindex(all_dates).ffill())
    bm_ma = bm.rolling(cfg.ma_window, min_periods=max(5, min(60, cfg.ma_window))).mean()
    exposure = pd.Series(1.0, index=all_dates)
    if cfg.risk_switch:
        exposure = pd.Series(np.where(bm > bm_ma, 1.0, cfg.off_exposure),
                             index=all_dates)

    W = cfg.weights
    fwd = close.shift(-int(cfg.fwd_days)) / close - 1

    reb = (reb_fn or month_end_dates)(dates)
    reb = reb[reb >= dates[min(len(dates) - 1, 5)]]
    exec_map = {}
    for i, d in enumerate(reb):
        j = dates.get_loc(d)
        if j + 1 < len(dates):
            exec_map[dates[j + 1]] = d

    ic_records, weights_store = [], {}
    prev_picks: list[str] = []
    for d in reb:
        elig = (close.loc[d].notna() & adjopen.loc[d].notna()
                & (listed.loc[d] >= cfg.min_list_days)
                & (amt20.loc[d] >= cfg.min_amt20)
                & (close.loc[d] >= cfg.min_price)
                & (~is_st)
                & (p["float_mv"].loc[d] > 0))
        if cfg.quality_screen and "roe" in p:
            roe_d = p["roe"].loc[d]
            gm_d = p["gm"].loc[d] if "gm" in p else None
            gate = roe_d > 0
            if gm_d is not None:
                gate = gate & (gm_d > 0)
            elig = elig & gate.reindex(codes).fillna(False).astype(bool)
        tot_s = pd.Series(0.0, index=codes)
        tot_w = pd.Series(0.0, index=codes)
        for k, v in raw.items():
            wk = float(W.get(k, 0.0))
            if wk <= 0:
                continue
            s = _cs_standardize(v.loc[d].where(elig), ind)
            ok = s.notna()
            tot_s = tot_s.add((wk * s).fillna(0.0), fill_value=0.0)
            tot_w = tot_w.add((ok * wk).astype(float), fill_value=0.0)
        sc = (tot_s / tot_w.where(tot_w > 0)).reindex(codes)
        # 质量倾斜：把 ROE / 毛利率 合成的质量分加在六因子之外的“外挂项”，
        # 不参与 tot_w 归一，因此不会稀释原有因子的相对权重。
        if cfg.quality_tilt > 0 and "roe" in raw:
            q = 0.5 * _cs_standardize(raw["roe"].loc[d].where(elig), ind)
            if "gm" in raw:
                q = q.add(0.5 * _cs_standardize(raw["gm"].loc[d].where(elig), ind),
                          fill_value=0.0)
            sc = sc + cfg.quality_tilt * q.reindex(codes).fillna(0.0)

        tw = target_weights(sc, elig, ind, prev_picks, cfg)
        prev_picks = list(tw.index)

        fr = fwd.loc[d]
        base = pd.concat([sc.rename("s"), fr.rename("r")], axis=1, join="inner")
        base = base[base.index.isin(elig[elig].index)].dropna()
        if len(base) > 50:
            rec = {"date": d, "ic": _rank_corr(base["s"], base["r"]),
                   "n": int(len(base))}
            for k, v in raw.items():
                pair = pd.concat([v.loc[d].rename("f"), base["r"]], axis=1,
                                 join="inner").dropna()
                if len(pair) > 50:
                    rec[f"ic_{k}"] = _rank_corr(pair["f"], pair["r"])
            ic_records.append(rec)
        weights_store[d] = (tw, float(exposure.loc[d]))

    # ---------------- 日频撮合 ----------------
    cash, shares = 1.0, {}
    nav_list, pos_list, turnover_list = [], [], []
    close_v = close.values.astype("float64")
    open_v = adjopen.values.astype("float64")
    last_price = np.full(len(codes), np.nan)

    def limit_band(code: str) -> float:
        return 0.196 if code[:2] in ("30", "68") else 0.098

    for i, d in enumerate(dates):
        px = close_v[i]
        # 此刻 last_price 仍是上一交易日收盘价（避免用当日收盘做当日开盘的调仓，防止前视）

        if d in exec_map and exec_map[d] in weights_store:
            tw, expo = weights_store[exec_map[d]]
            mv0 = sum(sh * last_price[col_pos[c]] for c, sh in shares.items()
                      if not np.isnan(last_price[col_pos[c]]))
            total = cash + mv0
            tgt_val = {c: float(tw[c]) * expo * total for c in tw.index}
            op = open_v[i]
            prev_close = close_v[i - 1] if i > 0 else op
            traded = 0.0

            for c in list(shares.keys()):
                tgt = tgt_val.get(c, 0.0)
                o = op[col_pos[c]]
                if np.isnan(o) or o <= 0:
                    continue
                cur = shares[c] * o
                if cur - tgt <= 0.005 * total:
                    continue
                pc = prev_close[col_pos[c]]
                if np.isnan(pc) or o / pc - 1 < -limit_band(c):
                    continue
                sell_sh = min(shares[c], (cur - tgt) / o)
                if sell_sh <= 0:
                    continue
                cash += sell_sh * o * (1 - cfg.slippage) * (1 - cfg.commission - cfg.stamp)
                shares[c] -= sell_sh
                traded += sell_sh * o
                if shares[c] < 1e-8:
                    del shares[c]

            for c, tgt in tgt_val.items():
                o = op[col_pos[c]]
                if np.isnan(o) or o <= 0:
                    continue
                pc = prev_close[col_pos[c]]
                if np.isnan(pc) or o / pc - 1 > limit_band(c):
                    continue
                cur = shares.get(c, 0.0) * o
                if tgt - cur <= 0.005 * total:
                    continue
                budget = min(tgt - cur, cash)
                if budget <= 0:
                    continue
                buy_sh = budget / (o * (1 + cfg.slippage) * (1 + cfg.commission))
                cash -= buy_sh * o * (1 + cfg.slippage) * (1 + cfg.commission)
                shares[c] = shares.get(c, 0.0) + buy_sh
                traded += buy_sh * o

            if total > 0:
                turnover_list.append({"date": d, "turnover": traded / (2 * total)})

        last_price = np.where(np.isnan(px), last_price, px)
        cash *= (1 + cfg.rf / 252)
        mv = sum(sh * last_price[col_pos[c]] for c, sh in shares.items()
                 if not np.isnan(last_price[col_pos[c]]))
        nav_list.append(cash + mv)
        pos_list.append(len(shares))

    nav = pd.Series(nav_list, index=dates, name="nav")
    if verbose:
        print(f"  调仓 {len(turnover_list)} 次，期末持仓 {pos_list[-1]} 只，"
              f"净值 {nav.iloc[-1]:.3f}")

    res = evaluate(nav, bm.reindex(dates), turnover_list, cfg, dates)
    last_d = reb[-1]
    last_w = weights_store[last_d][0]
    name_map = uni.set_index("code")["name"]
    res["last_portfolio"] = {
        "date": str(last_d.date()),
        "weights": {c: round(float(w), 4) for c, w in last_w.items()},
        "names": {c: str(name_map.get(c, "")) for c in last_w.index},
        "industry": {c: str(ind.get(c, "")) for c in last_w.index},
        "exposure": float(weights_store[last_d][1]),
    }
    res.update(nav=nav, bench=bm.reindex(dates), cfg=asdict(cfg),
               ic_df=pd.DataFrame(ic_records),
               turnover_df=pd.DataFrame(turnover_list),
               rebalance_dates=list(reb))
    return res


def evaluate(nav, bm, turnover_list, cfg, dates) -> dict:
    r = nav.pct_change().dropna()
    br = bm.pct_change().dropna()
    n_years = (dates[-1] - dates[0]).days / 365.25
    cagr = nav.iloc[-1] ** (1 / n_years) - 1
    bcagr = (bm.iloc[-1] / bm.iloc[0]) ** (1 / n_years) - 1
    vol = r.std() * np.sqrt(252)
    sharpe = (cagr - cfg.rf) / vol if vol > 0 else np.nan
    dd = nav / nav.cummax() - 1
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    ann_ex = cagr - bcagr
    te = (r - br).std() * np.sqrt(252)
    ir = ann_ex / te if te > 0 else np.nan
    beta = float(np.cov(r, br)[0, 1] / np.var(br))

    m = nav.resample("ME").last().pct_change().dropna()
    bmm = bm.resample("ME").last().pct_change().reindex(m.index)
    to = pd.DataFrame(turnover_list)

    # 分年度：用日收益按年复利，避免首年 NaN 与跨年边界误差
    def yearly_ret(series: pd.Series) -> pd.Series:
        rr = series.pct_change()
        g = rr.groupby(rr.index.year)
        return g.apply(lambda x: (1 + x.fillna(0)).prod() - 1)

    yr = pd.DataFrame({"策略": yearly_ret(nav), "基准": yearly_ret(bm)})
    yr["超额"] = yr["策略"] - yr["基准"]

    return dict(
        start=str(dates[0].date()), end=str(dates[-1].date()),
        years=round(n_years, 2),
        total_return=round(float(nav.iloc[-1] - 1), 4),
        benchmark_total=round(float(bm.iloc[-1] / bm.iloc[0] - 1), 4),
        cagr=round(float(cagr), 4), bench_cagr=round(float(bcagr), 4),
        ann_excess=round(float(ann_ex), 4),
        vol=round(float(vol), 4), sharpe=round(float(sharpe), 2),
        mdd=round(mdd, 4), calmar=round(float(calmar), 2),
        ir=round(float(ir), 2), beta=round(beta, 2),
        win_rate_month=round(float((m > 0).mean()), 3),
        win_rate_excess=round(float((m.values > bmm.values).mean()), 3),
        ann_turnover=round(float(to["turnover"].sum() / n_years), 2) if len(to) else np.nan,
        yearly={int(k): {kk: round(float(vv), 4) for kk, vv in v.items()}
                for k, v in yr.iterrows()},
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--risk-switch", action="store_true",
                    help="启用中证800 200日均线择时（默认关闭）")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    cfg = Config(start=args.start, end=args.end, top_n=args.top_n,
                 risk_switch=args.risk_switch)
    print(f"回测 {cfg.start} ~ {cfg.end}  top_n={cfg.top_n} "
          f"risk_switch={cfg.risk_switch}")
    res = run(cfg)

    OUT.mkdir(parents=True, exist_ok=True)
    tag = args.tag or cfg.label()
    pd.DataFrame({"nav": res["nav"], "bench": res["bench"]}).to_csv(OUT / f"nav_{tag}.csv")
    res["ic_df"].to_csv(OUT / f"ic_{tag}.csv", index=False)
    res["turnover_df"].to_csv(OUT / f"turnover_{tag}.csv", index=False)
    meta = {k: v for k, v in res.items()
            if k not in ("nav", "bench", "ic_df", "turnover_df", "cfg",
                         "rebalance_dates")}
    # 把实际跑这份回测的参数一并写进去，前端「回测表现」页要据此做参数说明。
    # 只保留标量/小字典，避免 meta 里塞进面板数据。
    meta["cfg"] = {k: v for k, v in res["cfg"].items()
                   if isinstance(v, (int, float, str, bool, dict))}
    with open(OUT / f"metrics_{tag}.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    keys = ("start", "end", "cagr", "bench_cagr", "ann_excess", "sharpe",
            "mdd", "ir", "ann_turnover")
    print(json.dumps({k: meta[k] for k in keys}, ensure_ascii=False, indent=2))
    print("分年度：")
    print(pd.DataFrame(meta["yearly"]).T.to_string())


if __name__ == "__main__":
    main()
