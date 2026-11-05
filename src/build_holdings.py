# -*- coding: utf-8 -*-
"""抓股东户数，构建「持仓拥挤度」面板。

为什么用股东户数：A 股没有公开的逐日机构持仓，但每家公司按季度披露股东户数
（东财 RPT_HOLDERNUM_DET 带 HOLD_NOTICE_DATE 公告日，可做 Point-in-Time）。
股东户数↓ = 筹码向少数人集中 = 持仓变拥挤；户数↑ = 筹码分散 = 拥挤度下降。

输出
----
data/holder_num.parquet        原始季度长表
data/panel/hold_crowd.parquet  逐日「拥挤度」∈[0,1]（越大越拥挤）
"""
from __future__ import annotations

import json
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
     "Referer": "https://data.eastmoney.com/gdhs/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,HOLD_NOTICE_DATE,"
        "HOLDER_NUM,PRE_HOLDER_NUM,HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,"
        "AVG_MARKET_CAP,TOTAL_MARKET_CAP")
PAGE = 500
START = "2018-03-31"


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


def quarter_ends(start: str) -> list[str]:
    s = pd.Timestamp(start)
    e = pd.Timestamp.now().normalize()
    out = []
    for y in range(s.year, e.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            ts = pd.Timestamp(y, m, d)
            if ts < s or ts > e:
                continue
            out.append(str(ts.date()))
    return out


def fetch_date(date: str) -> list[dict]:
    flt = urllib.parse.quote(f"(END_DATE='{date}')")
    rows, page = [], 1
    while True:
        url = (f"{BASE}?reportName=RPT_HOLDERNUM_DET&columns={COLS}"
               f"&filter={flt}&pageNumber={page}&pageSize={PAGE}"
               "&sortColumns=SECURITY_CODE&sortTypes=1")
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
        time.sleep(0.12)
    return rows


def main():
    dates = quarter_ends(START)
    print(f"[1/3] 抓股东户数 {START} ~ 今天（{len(dates)} 个报告期）")
    all_rows: list[dict] = []
    for i, d in enumerate(dates, 1):
        rows = fetch_date(d)
        all_rows.extend(rows)
        print(f"      {i:2d}/{len(dates)} {d}  {len(rows)} 条", flush=True)
        time.sleep(0.2)
    if not all_rows:
        print("没抓到数据")
        return

    df = pd.DataFrame(all_rows)
    df["code"] = df["SECURITY_CODE"].astype(str)
    df["end_date"] = pd.to_datetime(df["END_DATE"], errors="coerce")
    df["notice"] = pd.to_datetime(df["HOLD_NOTICE_DATE"], errors="coerce")
    for c in ("HOLDER_NUM", "HOLDER_NUM_RATIO", "AVG_MARKET_CAP"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["end_date", "notice"])
    df = df.drop_duplicates(["code", "end_date"], keep="last")
    raw = df[["code", "SECURITY_NAME_ABBR", "end_date", "notice",
              "HOLDER_NUM", "HOLDER_NUM_RATIO", "AVG_MARKET_CAP"]]
    raw.to_parquet(DATA / "holder_num.parquet", index=False)
    print(f"      去重后 {len(raw):,} 条，{raw['code'].nunique()} 只股票，"
          f"{raw['end_date'].min().date()} ~ {raw['end_date'].max().date()}")
    ok = raw["HOLDER_NUM_RATIO"].notna()
    print(f"      环比变化有效 {ok.sum():,} 条 ({ok.mean()*100:.1f}%)，"
          f"中位 {raw.loc[ok,'HOLDER_NUM_RATIO'].median():+.1f}%")

    print("[2/3] 按公告日铺到交易日")
    close = pd.read_parquet(PANEL / "close.parquet")
    days, cols = close.index, close.columns
    ev = raw.dropna(subset=["HOLDER_NUM_RATIO"]).copy()
    # 同一报告期内的截面排名：户数降幅越大（ratio 越负）越拥挤
    ev["crowd"] = (-ev["HOLDER_NUM_RATIO"]).groupby(ev["end_date"]).rank(pct=True)
    ev = ev.sort_values(["notice", "end_date"]).drop_duplicates(
        ["code", "notice"], keep="last")

    # 公告日 × 股票 的宽表 → 对齐交易日 → 前向填充（最多 150 个交易日）
    wide = ev.pivot_table(index="notice", columns="code", values="crowd",
                          aggfunc="last")
    idx = days.union(wide.index).sort_values()
    panel = (wide.reindex(idx).ffill(limit=150)
                 .reindex(index=days, columns=cols).astype("float32"))
    panel.index.name = "date"
    panel.to_parquet(PANEL / "hold_crowd.parquet")
    cov = panel.notna().mean().mean()
    print(f"      hold_crowd {panel.shape[0]} 交易日 x {panel.shape[1]} 标的，"
          f"平均覆盖 {cov*100:.1f}%")
    last = panel.iloc[-1].dropna()
    if len(last):
        print(f"      最新截面 {len(last)} 只有值，中位 {last.median():.2f}")
    print("      已写出", PANEL / "hold_crowd.parquet")


if __name__ == "__main__":
    main()