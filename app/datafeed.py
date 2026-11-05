# -*- coding: utf-8 -*-
"""增量更新数据：只补最新几天的行情与估值，不重下全历史。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, storage

import build_dataset as bd  # src/build_dataset.py

PANELS = ["close", "adjopen", "turnover", "amount", "pct_chg",
          "pe_ttm", "pb", "float_mv"]
FUND_PANELS = ["roe", "gm", "yoy_profit"]
LOOKBACK_DAYS = 15


AM_CLOSE = dt_time(11, 30)      # 上午收盘
PM_CLOSE = dt_time(15, 0)       # 下午收盘


def _cutoff(now: "datetime | None" = None) -> pd.Timestamp:
    """允许纳入的最大日期（不含）。

    A 股 11:30 上午收盘、15:00 下午收盘。这两个时点之后，当日 bar 就是
    “当前最新真实价”：

    - 11:30 之后（午休/午后/收盘后）：纳入当日，点更新就能拿到当天价格；
    - 11:30 之前（盘前/上午盘中）：不含当日，避免半根 bar 污染面板。

    15:00 收盘后的那次更新会把盘中的快照值覆盖成最终收盘价
    （_merge_panel 对同日单元格按“有新值就覆盖”处理）。
    """
    now = now or datetime.now()
    today = pd.Timestamp(now.date())
    if now.time() >= AM_CLOSE:
        return today + pd.Timedelta(days=1)
    return today


_CACHE: dict = {"t": 0.0, "v": None}


def panel_status(force: bool = False) -> dict:
    """数据状态。last_date = 行情与估值都齐、且已收盘的最新交易日。"""
    now = time.time()
    if not force and _CACHE["v"] is not None and now - _CACHE["t"] < 60:
        return _CACHE["v"]
    f = config.PANEL / "close.parquet"
    vf = config.PANEL / "float_mv.parquet"
    if not f.exists():
        st = {"ready": False, "last_date": None, "n_stocks": 0, "updated": None}
        _CACHE.update(t=now, v=st)
        return st
    close = pd.read_parquet(f)
    last = close.index.max()
    if vf.exists():
        val_idx = pd.read_parquet(vf).index
        complete = close.index.intersection(val_idx)
        complete = complete[complete < _cutoff()]
        if len(complete):
            last = complete.max()
    st = {
        "ready": True,
        "last_date": str(last.date()),
        "raw_last_date": str(close.index.max().date()),
        "first_date": str(close.index.min().date()),
        "n_stocks": int(close.shape[1]),
        "n_rows": int(close.shape[0]),
        "updated": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }
    _CACHE.update(t=now, v=st)
    return st


def _merge_panel(name: str, inc: pd.DataFrame) -> int:
    """把增量宽表合并进面板，返回新增交易日数。"""
    path = config.PANEL / f"{name}.parquet"
    base = pd.read_parquet(path)
    if inc.empty:
        return 0
    inc = inc.dropna(how="all")
    if inc.empty:
        return 0

    new_cols = inc.columns.difference(base.columns)
    if len(new_cols):
        add = pd.DataFrame(np.nan, index=base.index, columns=new_cols,
                           dtype="float32")
        base = pd.concat([base, add], axis=1)
    inc = inc.reindex(columns=base.columns)

    new_rows = inc.index.difference(base.index)
    common = inc.index.intersection(base.index)
    if len(common):
        # 只覆盖增量里有值的单元格；停牌股在增量里是 NaN，必须保留原值
        sub = inc.loc[common, :]
        old = base.loc[common, :]
        base.loc[common, :] = sub.where(sub.notna(), old).to_numpy(dtype="float32")
    if len(new_rows):
        base = pd.concat([base, inc.loc[new_rows]], axis=0)
    base = base.sort_index().astype("float32")
    base.index.name = "date"
    base.to_parquet(path)
    return len(new_rows)


def _extend_fundamentals() -> None:
    """把季度面板前向填充到最新行情日。

    build_fundamentals.py 是按“当时的交易日历”铺值的；行情每天往后加一天，
    roe/gm/yoy_profit 就少一天，signals.generate() 里 .loc[最新信号日] 会
    KeyError。季报在下一个公告日之前不变，所以按公告日语义 ffill 是对的。
    """
    close_path = config.PANEL / "close.parquet"
    if not close_path.exists():
        return
    close = pd.read_parquet(close_path)
    days, cols = close.index, close.columns
    if not len(days):
        return
    for k in FUND_PANELS:
        f = config.PANEL / f"{k}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        if df.index.max() >= days.max():
            continue
        new = (df.reindex(index=df.index.union(days)).ffill()
                 .reindex(index=days, columns=cols).astype("float32"))
        new.index.name = "date"
        new.to_parquet(f)
        storage.log(f"{k} 面板补齐到 {new.index.max().date()}")


def _fill_valuation_gap() -> None:
    """估值接口是日频 EOD，午间/刚收盘时当日估值（PE/PB/流通市值）往往还没发布。

    只补行情的话 panel_status 会停在昨天（它取 close∩float_mv），信号也依赖
    float_mv>0 才能选股。这里用「前一交易日估值 × 当日收盘/前收」补出当日快照
    —— 流通市值、PE、PB 在股本和盈利不变时确实与股价成正比，盘中近似成立，
    收盘后接口一发新值就会被 _merge_panel 覆盖成真值。
    """
    close_path = config.PANEL / "close.parquet"
    if not close_path.exists():
        return
    close = pd.read_parquet(close_path)
    for name in ("pe_ttm", "pb", "float_mv"):
        f = config.PANEL / f"{name}.parquet"
        if not f.exists():
            continue
        v = pd.read_parquet(f)
        gap = close.index.difference(v.index)
        if not len(gap):
            continue
        filled = 0
        for d in gap:
            prev = v.index[v.index < d]
            if not len(prev):
                continue
            p = prev.max()
            c0 = close.loc[p].reindex(v.columns)
            c1 = close.loc[d].reindex(v.columns)
            ratio = (c1 / c0).replace([np.inf, -np.inf], np.nan)
            v.loc[d] = (v.loc[p] * ratio).to_numpy(dtype="float32")
            filled += 1
        if not filled:
            continue
        v = v.sort_index().astype("float32")
        v.index.name = "date"
        v.to_parquet(f)
        storage.log(f"{name} 按收盘价比例补齐 {filled} 个交易日")


def update(progress_cb=None, workers: int = 10) -> dict:
    """拉取最近 LOOKBACK_DAYS 天数据并合并。"""
    cb = progress_cb or (lambda p, m="": None)
    cutoff = _cutoff()
    start = (cutoff - pd.Timedelta(days=LOOKBACK_DAYS + 20)).strftime("%Y-%m-%d")

    cb(0.02, "刷新股票列表…")
    uni = bd.fetch_universe()
    uni.to_parquet(config.DATA / "universe.parquet", index=False)

    cb(0.05, "刷新指数…")
    _refresh_index(start)

    recs = uni[["code", "market"]].to_dict("records")
    kl_rows, val_rows = [], []
    done = 0
    total = len(recs)

    def one(rec):
        k = bd.fetch_kline(rec, start, "2099-12-31")
        v = bd.fetch_valuation(rec["code"], start)
        return k, v

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, r) for r in recs]
        for fu in as_completed(futs):
            done += 1
            try:
                k, v = fu.result()
                if len(k):
                    kl_rows.append(k)
                if len(v):
                    val_rows.append(v)
            except Exception as e:  # noqa: BLE001
                storage.log(f"fetch error: {e}")
            if done % 50 == 0:
                cb(0.05 + 0.75 * done / total, f"抓取 {done}/{total}")

    cb(0.82, "合并行情面板…")
    added = 0
    if kl_rows:
        kl = pd.concat(kl_rows, ignore_index=True)
        kl = kl[kl["date"] < cutoff].drop_duplicates(["date", "code"])
        if len(kl):
            for src, dst in [("close", "close"), ("open", "adjopen"),
                             ("turnover", "turnover"), ("amount", "amount"),
                             ("pct_chg", "pct_chg")]:
                w = kl.set_index(["date", "code"])[src].unstack("code")
                added = max(added, _merge_panel(dst, w.astype("float32")))
            storage.log(f"行情合并完成，新增 {added} 个交易日")

    cb(0.93, "合并估值面板…")
    if val_rows:
        val = pd.concat(val_rows, ignore_index=True)
        val = val[val["date"] < cutoff].drop_duplicates(["date", "code"])
        if len(val):
            for c in ("pe_ttm", "pb", "float_mv"):
                w = val.set_index(["date", "code"])[c].unstack("code")
                _merge_panel(c, w.astype("float32"))

    _fill_valuation_gap()
    _extend_fundamentals()

    cb(1.0, "完成")
    st = panel_status(force=True)
    storage.log(f"update done: last={st['last_date']} stocks={st['n_stocks']}")
    return {"added_dates": added, **st}


def _refresh_index(start: str):
    rows = []
    for name, sid in bd.INDEX_LIST.items():
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={sid}&fields1=f1,f2,f3,f4,f5,f6&fields2={bd.KLINE_FIELDS}"
               f"&klt=101&fqt=1&beg={start.replace('-','')}&end=20991231")
        d = bd._get(url)
        kl = ((d or {}).get("data") or {}).get("klines") or []
        if not kl:
            continue
        df = pd.DataFrame([s.split(",") for s in kl], columns=bd.KLINE_COLS)
        df["index"] = name
        for c in ("open", "close", "high", "low", "volume", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        rows.append(df[["date", "index", "open", "close", "high", "low",
                        "volume", "amount"]])
    if not rows:
        return
    new = pd.concat(rows, ignore_index=True)
    path = config.DATA / "index.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(["date", "index"], keep="last").sort_values(
        ["index", "date"])
    new.to_parquet(path, index=False)
