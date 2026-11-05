# -*- coding: utf-8 -*-
"""抓业绩预告，构建「业绩预告正面占比」面板（分析师上调比例的替代）。

为什么要替代：券商研报接口（reportapi.eastmoney.com）批量抓取后会限流，
且 predictThisYearEps 在不带全参数时大量为空。公司自己发的业绩预告
数据量小得多、是硬信号，也更适合做「预期修正」。

因子定义
--------
每条预告按类型打分：

    正面 +1：预增、略增、扭亏、续盈、减亏
    负面 −1：预减、略减、首亏、增亏、续亏
    中性  0：不确定

滚动 90 天窗口内：

    fcst_up = (正面条数 + 0.5) / (总条数 + 1)      ∈ [0, 1]，0.5 为中性

平滑同 rev_up，避免「只有一条预告且是预增」直接给 0.9。

输出
----
data/forecast_raw.parquet    原始预告长表
data/panel/fcst_up.parquet   逐日宽表（月末计算 + 前向填充）
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PANEL = DATA / "panel"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "Chrome/124.0 Safari/537.36",
     "Referer": "https://data.eastmoney.com/bbsj/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORT_DATE,"
        "PREDICT_TYPE,PREDICT_FINANCE,ADD_AMP_LOWER,ADD_AMP_UPPER,"
        "FORECAST_STATE,PREDICT_CONTENT")
POS = {"预增", "略增", "扭亏", "续盈", "减亏"}
NEG = {"预减", "略减", "首亏", "增亏", "续亏"}
WINDOW_DAYS = 90
START_YEAR = 2018


def _curl_get(url: str, timeout: int = 30):
    try:
        p = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-H", f"User-Agent: {H['User-Agent']}",
             "-H", f"Referer: {H['Referer']}", url],
            capture_output=True, timeout=timeout + 8)
        if p.returncode == 0 and p.stdout:
            return json.loads(p.stdout.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _get(url: str, retries: int = 3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=H)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            if i == 0:
                d = _curl_get(url)
                if d is not None:
                    return d
            time.sleep(0.5 * 2 ** i)
    return None


def fetch_year(year: int, page_size: int = 500) -> list[dict]:
    begin = f"{year}-01-01"
    end = f"{year}-12-31"
    flt = urllib.parse.quote(f"(NOTICE_DATE>='{begin}')(NOTICE_DATE<='{end}')")
    rows: list[dict] = []
    page = 1
    while True:
        url = (f"{BASE}?reportName=RPT_PUBLIC_OP_NEWPREDICT&columns={COLS}"
               f"&filter={flt}&pageNumber={page}&pageSize={page_size}"
               "&sortColumns=NOTICE_DATE&sortTypes=1")
        d = _get(url)
        if not d:
            break
        res = d.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        rows.extend(data)
        pages = int(res.get("pages") or 1)
        if page >= pages:
            break
        page += 1
        time.sleep(0.15)
    return rows


def main():
    year_now = pd.Timestamp.now().year
    print(f"[1/3] 抓取业绩预告 {START_YEAR} ~ {year_now}")
    all_rows: list[dict] = []
    for y in range(START_YEAR, year_now + 1):
        rows = fetch_year(y)
        all_rows.extend(rows)
        print(f"      {y}  {len(rows):,} 条", flush=True)
        time.sleep(0.3)
    if not all_rows:
        print("没抓到数据")
        return

    df = pd.DataFrame(all_rows)
    df["code"] = df["SECURITY_CODE"].astype(str)
    df["notice"] = pd.to_datetime(df["NOTICE_DATE"], errors="coerce")
    df["report"] = pd.to_datetime(df["REPORT_DATE"], errors="coerce")
    for c in ("ADD_AMP_LOWER", "ADD_AMP_UPPER"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["notice"])
    df = df.sort_values(["code", "notice"]).drop_duplicates(
        subset=["code", "report", "notice"], keep="last")

    score = np.where(df["PREDICT_TYPE"].isin(POS), 1,
                     np.where(df["PREDICT_TYPE"].isin(NEG), -1, 0))
    df["dir"] = score
    df["amp_mid"] = (df["ADD_AMP_LOWER"] + df["ADD_AMP_UPPER"]) / 2

    keep = ["code", "SECURITY_NAME_ABBR", "notice", "report", "PREDICT_TYPE",
            "PREDICT_FINANCE", "dir", "amp_mid"]
    raw = df[keep].rename(columns={"notice": "notice_date", "report": "report_date"})
    raw.drop(columns=["PREDICT_FINANCE"]).to_parquet(
        DATA / "forecast_raw.parquet", index=False)
    n_pos = int((raw["dir"] > 0).sum())
    n_neg = int((raw["dir"] < 0).sum())
    print(f"      去重后 {len(raw):,} 条，{raw['code'].nunique()} 只股票，"
          f"{raw['notice_date'].min().date()} ~ {raw['notice_date'].max().date()}")
    print(f"      正面 {n_pos:,} / 负面 {n_neg:,} / 中性 {len(raw)-n_pos-n_neg:,}")

    print("[2/3] 滚动 90 天 → 逐日面板")
    close = pd.read_parquet(PANEL / "close.parquet")
    days, cols = close.index, close.columns
    ev = raw[["code", "notice_date", "dir"]].dropna().sort_values("notice_date")
    ev_dates = ev["notice_date"].values

    months = pd.date_range(days[0], days[-1], freq="ME")
    rows = {}
    for m in months:
        lo = np.datetime64(m - pd.Timedelta(days=WINDOW_DAYS))
        hi = np.datetime64(m)
        i0 = np.searchsorted(ev_dates, lo, side="right")
        i1 = np.searchsorted(ev_dates, hi, side="right")
        win = ev.iloc[i0:i1]
        if not len(win):
            continue
        agg = win.groupby("code")["dir"].agg(
            pos=lambda s: int((s > 0).sum()), n="size")
        rows[m] = (agg["pos"] + 0.5) / (agg["n"] + 1)

    panel_m = pd.DataFrame(rows).T
    panel_m.index = pd.to_datetime(panel_m.index)
    panel = panel_m.reindex(columns=cols).reindex(days).ffill(limit=90)
    panel = panel.astype("float32")
    panel.index.name = "date"
    panel.to_parquet(PANEL / "fcst_up.parquet")

    cov = panel.notna().mean().mean()
    last = panel.iloc[-1].dropna()
    print(f"      fcst_up {panel.shape[0]} 交易日 x {panel.shape[1]} 标的，"
          f"平均覆盖 {cov*100:.1f}%")
    if len(last):
        print(f"      最新截面 {len(last)} 只有值，中位 {last.median():.2f}")
    print("      已写出", PANEL / "fcst_up.parquet")


if __name__ == "__main__":
    main()
