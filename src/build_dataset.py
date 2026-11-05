# -*- coding: utf-8 -*-
"""
数据落地脚本：从东方财富公开接口抓取 A 股全市场日线与每日估值，落成本地 parquet。

数据源
------
1. 股票列表 & 行业 : push2.eastmoney.com/api/qt/clist/get
2. 前复权日线      : push2his.eastmoney.com/api/qt/stock/kline/get  (klt=101, fqt=1)
3. 每日估值        : datacenter-web.eastmoney.com  RPT_VALUEANALYSIS_DET
                     (PE_TTM / PB_MRQ / PS_TTM / 总市值 / 流通股本)
4. 指数日线        : 同上 kline 接口，secid 形如 1.000906

用法
----
    python build_dataset.py --start 2016-01-01 --workers 6
断点续传：已存在的单只股票 parquet 会自动跳过。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
KL_DIR = RAW / "kl"
VAL_DIR = RAW / "val"
INDEX_DIR = RAW / "index"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
KLINE_COLS = ["date", "open", "close", "high", "low",
              "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]

INDEX_LIST = {
    "zz800": "1.000906",   # 中证800（基准）
    "hs300": "1.000300",   # 沪深300
    "zz500": "1.000905",   # 中证500
    "zz1000": "1.000852",  # 中证1000
    "cyb": "0.399006",     # 创业板指
}

_ss = None


# ---------------------------------------------------------------- HTTP
def _curl_get(url: str, referer: str, timeout: int = 25):
    """urllib 兜底：用系统 curl 再要一次。

    东财 datacenter-web 域名偶尔会对 Python 的 TLS ClientHello 直接
    回 EOF（curl 却能过），表现为 SSLEOFError。这里用子进程 curl 绕过去，
    Windows 10+/Linux/macOS 都自带 curl。
    """
    try:
        p = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-H", f"User-Agent: {UA}", "-H", f"Referer: {referer}", url],
            capture_output=True, timeout=timeout + 8)
        if p.returncode == 0 and p.stdout:
            return json.loads(p.stdout.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _get(url: str, referer: str = "https://quote.eastmoney.com/",
         retries: int = 4, timeout: int = 25):
    """带指数退避的 JSON GET；urllib 失败时用 curl 兜一次。"""
    headers = {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            last = e
            if i == 0:
                d = _curl_get(url, referer, timeout)
                if d is not None:
                    return d
            time.sleep((0.4 * 2 ** i) + random.random() * 0.3)
    print(f"  [warn] GET failed after {retries}: {url[:90]} -> {last}", file=sys.stderr)
    return None


def secid(code: str, mkt: int) -> str:
    return f"{1 if int(mkt) == 1 else 0}.{code}"


def is_a_share(code: str) -> bool:
    """剔除北交所(4/8/9开头)、B股、退市整理。只留沪深主板/创业板/科创板。"""
    if not code.isdigit() or len(code) != 6:
        return False
    return code[:3] in {
        "600", "601", "603", "605", "688", "689",          # 沪
        "000", "001", "002", "003", "300", "301",          # 深
    }


# ---------------------------------------------------------------- 列表
def fetch_universe() -> pd.DataFrame:
    rows, pn = [], 1
    while True:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f12,f13,f14,f20,f21,f26,f100")
        d = _get(url)
        diff = ((d or {}).get("data") or {}).get("diff") or []
        if not diff:
            break
        rows += diff
        pn += 1
        if len(rows) > 12000:
            break
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f13": "market", "f14": "name", "f20": "total_mv",
        "f21": "float_mv", "f26": "list_date", "f100": "industry",
    })
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].map(is_a_share)].copy()
    df["list_date"] = pd.to_datetime(df["list_date"].astype(str),
                                     format="%Y%m%d", errors="coerce")
    df["market"] = pd.to_numeric(df["market"], errors="coerce").fillna(0).astype(int)
    for c in ("total_mv", "float_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates("code").sort_values("code").reset_index(drop=True)
    return df


# ---------------------------------------------------------------- 日线
def fetch_kline(rec: dict, start: str, end: str) -> pd.DataFrame:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid(rec['code'], rec['market'])}"
           "&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2={KLINE_FIELDS}&klt=101&fqt=1"
           f"&beg={start.replace('-', '')}&end={end.replace('-', '')}")
    d = _get(url)
    kl = ((d or {}).get("data") or {}).get("klines") or []
    if not kl:
        return pd.DataFrame()
    arr = [s.split(",") for s in kl]
    df = pd.DataFrame(arr, columns=KLINE_COLS)
    df["code"] = rec["code"]
    for c in ["open", "close", "high", "low", "volume", "amount",
              "amplitude", "pct_chg", "change", "turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------- 估值
VAL_COLS = ["SECURITY_CODE", "TRADE_DATE", "PE_TTM", "PB_MRQ", "PS_TTM",
            "TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A", "TOTAL_SHARES",
            "FREE_SHARES_A", "CLOSE_PRICE"]


def fetch_valuation(code: str, start: str) -> pd.DataFrame:
    cols = ",".join(VAL_COLS)
    flt = urllib.parse.quote(f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{start}\')')
    base = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPT_VALUEANALYSIS_DET"
            f"&columns={cols}&filter={flt}"
            "&pageNumber={pn}&pageSize=1000&sortColumns=TRADE_DATE&sortTypes=1")
    rows, pn = [], 1
    while True:
        d = _get(base.format(pn=pn), referer="https://data.eastmoney.com/")
        res = (d or {}).get("result") or {}
        data = res.get("data") or []
        rows += data
        pages = res.get("pages") or 1
        if pn >= pages or not data:
            break
        pn += 1
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for c in VAL_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[VAL_COLS].copy()
    df["TRADE_DATE"] = pd.to_datetime(df["TRADE_DATE"]).dt.normalize()
    for c in VAL_COLS[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code"] = code
    return df.rename(columns={
        "TRADE_DATE": "date", "PE_TTM": "pe_ttm", "PB_MRQ": "pb",
        "PS_TTM": "ps", "TOTAL_MARKET_CAP": "total_mv",
        "NOTLIMITED_MARKETCAP_A": "float_mv", "TOTAL_SHARES": "total_shares",
        "FREE_SHARES_A": "free_shares", "CLOSE_PRICE": "close_raw",
    }).drop(columns=["SECURITY_CODE"])


# ---------------------------------------------------------------- 主流程
def _one(rec, start, end, skip_kl, skip_val):
    p_kl = KL_DIR / f"{rec['code']}.parquet"
    p_val = VAL_DIR / f"{rec['code']}.parquet"
    ok = True
    if not (skip_kl and p_kl.exists()):
        try:
            df = fetch_kline(rec, start, end)
            if len(df):
                df.to_parquet(p_kl, index=False)
            else:
                df.to_parquet(p_kl, index=False)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] kline {rec['code']}: {e}", file=sys.stderr)
            ok = False
    if not (skip_val and p_val.exists()):
        try:
            dv = fetch_valuation(rec["code"], start)
            dv.to_parquet(p_val, index=False)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] val {rec['code']}: {e}", file=sys.stderr)
            ok = False
    return rec["code"], ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-skip", action="store_true")
    args = ap.parse_args()

    for d in (KL_DIR, VAL_DIR, INDEX_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print("[1/3] 抓取股票列表 ...")
    uni = fetch_universe()
    if args.limit:
        uni = uni.head(args.limit)
    uni.to_parquet(ROOT / "data" / "universe.parquet", index=False)
    print(f"      A 股标的 {len(uni)} 只")

    print("[2/3] 抓取指数日线 ...")
    idx = []
    for name, sid in INDEX_LIST.items():
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={sid}&fields1=f1,f2,f3,f4,f5,f6&fields2={KLINE_FIELDS}"
               f"&klt=101&fqt=1&beg={args.start.replace('-', '')}&end=20261231")
        d = _get(url)
        kl = ((d or {}).get("data") or {}).get("klines") or []
        if not kl:
            print(f"      [warn] index {name} empty")
            continue
        df = pd.DataFrame([s.split(",") for s in kl], columns=KLINE_COLS)
        df["index"] = name
        for c in ["open", "close", "high", "low", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        idx.append(df[["date", "index", "open", "close", "high", "low",
                       "volume", "amount"]])
    pd.concat(idx, ignore_index=True).to_parquet(
        ROOT / "data" / "index.parquet", index=False)
    print("      指数 OK")

    print(f"[3/3] 并发抓取个股数据 (workers={args.workers}) ...")
    recs = uni[["code", "market"]].to_dict("records")
    skip_kl = not args.no_skip
    skip_val = not args.no_skip
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_one, r, args.start, args.end, skip_kl, skip_val)
                for r in recs]
        for f in as_completed(futs):
            done += 1
            if done % 200 == 0:
                el = time.time() - t0
                eta = el / done * (len(recs) - done)
                print(f"      {done}/{len(recs)}  {el/60:.1f}min  "
                      f"ETA {eta/60:.1f}min", flush=True)
    print(f"完成，用时 {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
