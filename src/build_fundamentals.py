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
import subprocess
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
        "XSMLL,YSTZ,SJLTZ,BASIC_EPS,MGJYXJJE")
URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
       "?reportName=RPT_LICO_FN_CPD"
       f"&columns={COLS}"
       "&filter=(REPORTDATE%3D%27{date}%27)"
       "&pageNumber={page}&pageSize=500"
       "&sortColumns=SECURITY_CODE&sortTypes=1")


def _curl_get(url: str, timeout: int = 30):
    """urllib 兜底：这个域名偶尔会对 Python 的 TLS 握手直接回 EOF。"""
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


def _get(url: str, retries: int = 4):
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
                   ("YSTZ", "yoy_rev"), ("SJLTZ", "yoy_profit"),
                   ("BASIC_EPS", "eps"), ("MGJYXJJE", "ocf_ps")]:
        df[dst] = pd.to_numeric(df[c], errors="coerce")
    out = df[["SECURITY_CODE", "report_date", "notice_date",
              "roe_raw", "gm", "yoy_rev", "yoy_profit", "eps", "ocf_ps"]]
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


def compute_sue(fund: pd.DataFrame, clip: float = 5.0) -> pd.Series:
    """标准化预期外盈利 SUE。

        Δ4   = 本期 EPS − 去年同期 EPS（同季度，累计口径可比）
        SUE  = Δ4 / std(过去 8 个季度的 Δ4)        ← 用 shift(1) 排除当期，避免自相关

    Bernard & Thomas 那套口径，不需要分析师一致预期，只用已披露财报，天然 PIT。
    """
    f = fund.sort_values(["code", "report_date"]).copy()
    g = f.groupby("code", sort=False)
    d4 = g["eps"].diff(4)
    sd = (d4.groupby(f["code"], sort=False)
          .transform(lambda x: x.rolling(8, min_periods=4).std().shift(1)))
    sue = (d4 / sd.where(sd > 0)).clip(-clip, clip)
    return sue.reindex(fund.index)


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
    # 盈利质量（应计因子）：经营现金流 / 净利润
    #   = 每股经营现金流 ÷ 基本每股收益
    # 两个都是「同一报告期累计值 ÷ 总股本」，相除正好等于 CFO/NI，
    # 不需要额外拿股本数据。亏损股（eps<=0）这个比值没意义，置空。
    fund["ocf_ni"] = (fund["ocf_ps"] /
                      fund["eps"].where(fund["eps"] > 0)).clip(-20, 20)
    fund["sue"] = compute_sue(fund)
    fund.to_parquet(DATA / "fundamentals.parquet", index=False)
    print(f"      合计 {len(fund):,} 条，{fund['code'].nunique()} 只股票")
    print(f"      ocf_ni 有效占比 {fund['ocf_ni'].notna().mean()*100:.1f}%（亏损股不计）")

    print("[2/3] 对齐到交易日 ...")
    close = pd.read_parquet(PANEL / "close.parquet")
    days = close.index
    for col, name in [("roe", "roe"), ("gm", "gm"),
                      ("yoy_profit", "yoy_profit"), ("ocf_ni", "ocf_ni"), ("sue", "sue")]:
        w = to_daily(fund, col, days).reindex(columns=close.columns)
        w.to_parquet(PANEL / f"{name}.parquet")
        cov = w.notna().any(axis=1).mean()
        print(f"      {name}: {w.shape[0]} 交易日 x {w.shape[1]} 标的，"
              f"平均覆盖率 {w.notna().mean().mean()*100:.1f}%")

    print("[3/3] 完成 ->", PANEL)


if __name__ == "__main__":
    main()
