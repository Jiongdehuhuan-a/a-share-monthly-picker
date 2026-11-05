# -*- coding: utf-8 -*-
"""
把 data/raw 下逐股票 parquet 合并成宽表（date x code）矩阵，供回测快速读取。

输出 data/panel/*.parquet
    close, adjopen, turnover, amount, pct_chg, pe_ttm, pb, float_mv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PANEL = ROOT / "data" / "panel"

KL_COLS = ["date", "code", "open", "close", "turnover", "amount", "pct_chg"]
VAL_COLS = ["date", "code", "pe_ttm", "pb", "float_mv"]


def _load(dirpath: Path, cols: list[str]) -> pd.DataFrame:
    files = sorted(dirpath.glob("*.parquet"))
    print(f"  读取 {len(files)} 个文件: {dirpath.name}")
    frames = []
    for i, f in enumerate(files):
        try:
            df = pd.read_parquet(f)
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] {f.name}: {e}", file=sys.stderr)
            continue
        if len(df) == 0:
            continue
        frames.append(df[[c for c in cols if c in df.columns]])
        if (i + 1) % 1500 == 0:
            print(f"    {i+1}/{len(files)}", flush=True)
    out = pd.concat(frames, ignore_index=True)
    out["code"] = out["code"].astype(str)
    return out


def to_wide(df: pd.DataFrame, col: str, dtype="float32") -> pd.DataFrame:
    s = df.set_index(["date", "code"])[col]
    w = s.unstack("code").sort_index()
    w = w.astype(dtype)
    w.index.name = "date"
    return w


def main():
    PANEL.mkdir(parents=True, exist_ok=True)

    print("[1/2] 合并日线 ...")
    kl = _load(RAW / "kl", KL_COLS)
    print(f"  日线总行数 {len(kl):,}")
    kl = kl.drop_duplicates(["date", "code"])
    for src, dst in [("close", "close"), ("open", "adjopen"),
                     ("turnover", "turnover"), ("amount", "amount"),
                     ("pct_chg", "pct_chg")]:
        w = to_wide(kl, src)
        w.to_parquet(PANEL / f"{dst}.parquet")
        print(f"  {dst}: {w.shape[0]} 交易日 x {w.shape[1]} 标的")
    del kl

    print("[2/2] 合并估值 ...")
    val = _load(RAW / "val", VAL_COLS)
    print(f"  估值总行数 {len(val):,}")
    val = val.drop_duplicates(["date", "code"])
    for c in ("pe_ttm", "pb", "float_mv"):
        w = to_wide(val, c)
        w.to_parquet(PANEL / f"{c}.parquet")
        print(f"  {c}: {w.shape[0]} 交易日 x {w.shape[1]} 标的")

    print("完成 ->", PANEL)


if __name__ == "__main__":
    main()
