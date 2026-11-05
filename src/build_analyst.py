# -*- coding: utf-8 -*-
"""抓取券商研报，构建「盈利预测上调比例」面板。

数据源：东财研报中心 reportapi.eastmoney.com/report/list
每篇研报带 stockCode / publishDate / predictThisYearEps / 评级变化。

因子定义
--------
对同一只股票、同一自然年（"本年" EPS 预测指向同一个财年）内的相邻两篇研报，
比较 predictThisYearEps：

    上调  eps_new >  eps_old × (1 + 0.1%)   → +1
    下调  eps_new <  eps_old × (1 − 0.1%)   → −1
    持平                                         →  0

再按滚动 90 天窗口统计：

    rev_up = 上调篇数 / (上调篇数 + 下调篇数)      ∈ [0, 1]，越高越是集体上调

窗口内没有带方向研报的股票为 NaN（该因子不参与打分，其他因子照常）。

输出
----
data/analyst_reports.parquet   原始研报长表（复用/排查用）
data/panel/rev_up.parquet      逐日宽表（月末计算 + 前向填充到交易日）
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PANEL = DATA / "panel"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "Chrome/124.0 Safari/537.36",
     "Referer": "https://data.eastmoney.com/report/"}
BASE = "https://reportapi.eastmoney.com/report/list"
START = "2018-10-01"
PAGE = 100
WINDOW_DAYS = 90


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


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """收拾混合类型列，否则 pyarrow 写 parquet 会报 int64 转换失败。"""
    df = df.copy()
    for c in ("ratingChange", "predictNextYearEps", "predictThisYearEps"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("stockCode", "stockName", "emRatingName", "lastEmRatingName",
              "orgSName", "title"):
        if c in df.columns:
            df[c] = df[c].astype(str)
    return df
    for c in df.columns:
        if df[c].dtype == object and df[c].map(type).eq(list).any():
            df[c] = df[c].astype(str)


def fetch_range(begin: str, end: str) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        url = (f"{BASE}?pageSize={PAGE}&pageNo={page}&qType=0"
               f"&beginTime={begin}&endTime={end}"
               "&fields=&rating=&ratingChange=&industryCode=*&code=*")
        d = _get(url)
        if not d:
            break
        data = d.get("data") or []
        if not data:
            break
        rows.extend(data)
        pages = int(math.ceil((d.get("hits") or 0) / PAGE))
        if page >= pages:
            break
        page += 1
        if page % 20 == 0:
            time.sleep(0.2)
    return rows


def quarters(start: str, end: str) -> list[tuple[str, str]]:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        nxt = min(cur + pd.DateOffset(months=3) - pd.Timedelta(days=1), e)
        out.append((str(cur.date()), str(nxt.date())))
        cur = nxt + pd.Timedelta(days=1)
    return out


def main():
    reuse = "--from-cache" in sys.argv
    cache = DATA / "analyst_reports.parquet"

    if reuse and cache.exists():
        df = pd.read_parquet(cache)
        print(f"[1/3] 复用缓存 {cache}（{len(df):,} 篇），跳过抓取")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["eps_fy"] = pd.to_numeric(df["eps_fy"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values(["stockCode", "date"])
    else:
        end = str(pd.Timestamp.now().normalize().date())
        wins = quarters(START, end)
        print(f"[1/3] 抓取研报 {START} ~ {end}（{len(wins)} 个季度窗口）")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        all_rows = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(fetch_range, b, e): (b, e) for b, e in wins}
            done = 0
            for fu in as_completed(futs):
                b, e = futs[fu]
                try:
                    rows = fu.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"      [warn] {b}~{e} 失败: {exc}")
                    rows = []
                all_rows.extend(rows)
                done += 1
                print(f"      {done:2d}/{len(wins)} {b}~{e}  {len(rows)} 篇", flush=True)
                if done % 8 == 0:
                    try:
                        normalize(pd.DataFrame(all_rows)).to_parquet(
                            DATA / "analyst_reports_partial.parquet", index=False)
                    except Exception as exc:  # noqa: BLE001
                        print(f"      [warn] 落盘失败: {exc}")

        if not all_rows:
            print("没抓到数据，退出")
            return

        df = pd.DataFrame(all_rows)
    keep = ["stockCode", "stockName", "publishDate", "predictThisYearEps",
            "predictNextYearEps", "emRatingName", "lastEmRatingName",
            "ratingChange", "orgSName", "title"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["publishDate"], errors="coerce")
    df["eps_fy"] = pd.to_numeric(df["predictThisYearEps"], errors="coerce")
    for c in ("stockCode", "stockName", "emRatingName", "lastEmRatingName",
              "orgSName", "title"):
        if c in df.columns:
            df[c] = df[c].astype(str)
    for c in ("ratingChange", "predictNextYearEps"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values(["stockCode", "date"])
    df = normalize(df)
    df.to_parquet(DATA / "analyst_reports.parquet", index=False)
    print(f"      合计 {len(df):,} 篇，{df['stockCode'].nunique()} 只股票，"
          f"{df['date'].min().date()} ~ {df['date'].max().date()}")

    print("[2/3] 计算每篇研报的预测方向")
    df["year"] = df["date"].dt.year
    g = df.groupby(["stockCode", "year"], sort=False)
    prev_eps = g["eps_fy"].shift(1)
    prev_date = g["date"].shift(1)
    gap = (df["date"] - prev_date).dt.days

    ok = (df["eps_fy"].notna() & prev_eps.notna()
          & (prev_eps.abs() > 1e-6) & (gap <= 240))
    delta = (df["eps_fy"] - prev_eps) / prev_eps.abs().clip(lower=1e-6)
    rev = np.full(len(df), np.nan)
    rev[ok.values] = np.where(delta[ok.values] > 0.001, 1,
                              np.where(delta[ok.values] < -0.001, -1, 0))
    df["rev"] = rev
    print(f"      有方向的样本 {int(np.isfinite(rev).sum()):,} 条 "
          f"(上调 {int((rev == 1).sum()):,} / 下调 {int((rev == -1).sum()):,} "
          f"/ 持平 {int((rev == 0).sum()):,})")

    print("[3/3] 滚动 90 天 → 逐日面板")
    ev = df[["stockCode", "date", "rev"]].dropna()
    ev = ev.sort_values("date")
    close = pd.read_parquet(PANEL / "close.parquet")
    days = close.index
    cols = close.columns

    months = pd.date_range(days[0], days[-1], freq="ME")
    rows = {}
    start_i = 0
    ev_dates = ev["date"].values
    for m in months:
        lo = np.datetime64(m - pd.Timedelta(days=WINDOW_DAYS))
        hi = np.datetime64(m)
        i0 = np.searchsorted(ev_dates, lo, side="right")
        i1 = np.searchsorted(ev_dates, hi, side="right")
        win = ev.iloc[i0:i1]
        if len(win):
            agg = win.groupby("stockCode")["rev"].agg(
                up=lambda s: int((s > 0).sum()),
                dn=lambda s: int((s < 0).sum()))
            agg["n"] = agg["up"] + agg["dn"]
            # 平滑：(上调 + 0.5) / (有方向篇数 + 1)，避免「1 篇上调 = 1.0」
            ratio = ((agg["up"] + 0.5) / (agg["n"] + 1)).where(agg["n"] >= 1)
            rows[m] = ratio
    panel_m = pd.DataFrame(rows).T
    panel_m.index = pd.to_datetime(panel_m.index)
    panel = panel_m.reindex(columns=cols).reindex(days).ffill(limit=60)
    panel = panel.astype("float32")
    panel.index.name = "date"
    panel.to_parquet(PANEL / "rev_up.parquet")

    cov = panel.notna().mean().mean()
    print(f"      rev_up 面板 {panel.shape[0]} 交易日 x {panel.shape[1]} 标的，"
          f"平均覆盖率 {cov*100:.1f}%")
    latest = panel.iloc[-1].dropna()
    if len(latest):
        print(f"      最新截面 {len(latest)} 只有值，"
              f"中位 {latest.median():.2f}，均值 {latest.mean():.2f}")
    print("      已写出", PANEL / "rev_up.parquet")


if __name__ == "__main__":
    main()