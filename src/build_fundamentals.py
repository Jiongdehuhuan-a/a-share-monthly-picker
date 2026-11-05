# -*- coding: utf-8 -*-
"""
抓取季度财务指标（ROE / 毛利率 / 营收净利同比），并生成逐日宽表。

关键点：用 **公告日 NOTICE_DATE** 而不是报告期做对齐，避免未来函数。
    2024Q4 报表报告期 2024-12-31，但 2025-03-15 才公告，
    所以 2025-03-14 的持仓决策只能用 2024Q3 的数据。

输出
    data/fundamentals.parquet          原始长表
    data/panel/roe.parquet             ROE（年化）逐日宽表
    data/panel/gm.parquet              销售毛利率 逐日宽表
    data/panel/yoy_profit.parquet      归母净利同比 逐日宽表
"""
from __future__ import annotations

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PANEL = DATA / "panel"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
     "Referer": "https://data.eastmoney.com/"}

COLS = ("SECURITY_CODE,REPORTDATE,NOTICE_DATE,WEIGHTAVG_ROE,"
        "XSMLL,YSTZ,SJLTZ")
URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
       "?reportName=RPT_LICO_FN_CPD"
       f"&columns={COLS}"
       "&filter=(REPORTDATE%3D%27{date}%27)"
       "&pageNumber={page}&pageSize=500"
       "&sortColumns=SECURITY_CODE&sortTypes=1")


def _get(url: str, retries: int = 4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=H)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            import time
            time.sleep(0.5 * 2 ** i)
    return None


def _is_a(code: str) -> bool:
    return (len(code) == 6 and code.isdigit()
            and code[:3] in {"600", "601", "603", "605", "688", "689",
                             "000", "001", "002", "003", "300", "301"})


def fetch_quarter(date: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        d = _get(URL.format(date=date, page=page))
        res = (d or {}).get("result") or {}
        data = res.get("data") or []
        rows += data
        pages = res.get("pages") or 1
        if page >= pages or not data:
            break
        page += 1
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[df["SECURITY_CODE"].astype(str).map(_is_a)].copy()
    df["report_date"] = pd.to_datetime(df["REPORTDATE"])
    df["notice_date"] = pd.to_datetime(df["NOTICE_DATE"])
    for c, dst in [("WEIGHTAVG_ROE", "roe_raw"), ("XSMLL", "gm"),
                   ("YSTZ", "yoy_rev"), ("SJLTZ", "yoy_profit")]:
        df[dst] = pd.to_numeric(df[c], errors="coerce")
    out = df[["SECURITY_CODE", "report_date", "notice_date",
              "roe_raw", "gm", "yoy_rev", "yoy_profit"]]
    return out.rename(columns={"SECURITY_CODE": "code"})


def quarters(start="2018-03-31", end=None) -> list[str]:
    end = end or pd.Timestamp.now().normalize()
    out = []
    for y in range(2018, 2027):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            ts = pd.Timestamp(y, m, d)
            if ts < pd.Timestamp(start) or ts > end:
                continue
            out.append(str(ts.date()))
    return out


def annualize_roe(df: pd.DataFrame) -> pd.Series:
    """把累计 ROE 年化：Q1×4，H1×2，Q3×4/3，年报×1。"""
    q = df["report_date"].dt.month.map({3: 1, 6: 2, 9: 3, 12: 4})
    mult = q.map({1: 4.0, 2: 2.0, 3: 4 / 3, 4: 1.0})
    return df["roe_raw"] * mult


def to_daily(fund: pd.DataFrame, col: str, trading_days: pd.DatetimeIndex) -> pd.DataFrame:
    """按公告日把季度值铺到交易日上（前向填充）。"""
    d = fund.dropna(subset=["notice_date", col])
    if d.empty:
        return pd.DataFrame(np.nan, index=trading_days, columns=[],
                            dtype="float32")
    wide = d.pivot_table(index="notice_date", columns="code", values=col,
                         aggfunc="last")
    idx = wide.index.union(trading_days).sort_values()
    wide = wide.reindex(idx).ffill().reindex(trading_days)
    wide.index.name = "date"
    return wide.astype("float32")


def main():
    print("[1/3] 抓取季度报表 ...")
    qs = quarters()
    print(f"      共 {len(qs)} 个报告期: {qs[0]} ~ {qs[-1]}")
    frames = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, df in enumerate(ex.map(fetch_quarter, qs)):
            frames.append(df)
            print(f"      {i+1}/{len(qs)} {qs[i]}  {len(df)} 条", flush=True)
    fund = pd.concat([f for f in frames if len(f)], ignore_index=True)
    fund = fund.drop_duplicates(["code", "report_date"], keep="last")
    fund["roe"] = annualize_roe(fund)
    fund.to_parquet(DATA / "fundamentals.parquet", index=False)
    print(f"      合计 {len(fund):,} 条，{fund['code'].nunique()} 只股票")

    print("[2/3] 对齐到交易日 ...")
    close = pd.read_parquet(PANEL / "close.parquet")
    days = close.index
    for col, name in [("roe", "roe"), ("gm", "gm"),
                      ("yoy_profit", "yoy_profit")]:
        w = to_daily(fund, col, days).reindex(columns=close.columns)
        w.to_parquet(PANEL / f"{name}.parquet")
        cov = w.notna().any(axis=1).mean()
        print(f"      {name}: {w.shape[0]} 交易日 x {w.shape[1]} 标的，"
              f"平均覆盖率 {w.notna().mean().mean()*100:.1f}%")

    print("[3/3] 完成 ->", PANEL)


if __name__ == "__main__":
    main()
