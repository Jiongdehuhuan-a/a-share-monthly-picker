# -*- coding: utf-8 -*-
"""
样本内 / 样本外切分检验：把 2019-2026 切成两段，看超额是否稳定。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest import Config, run

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

SPLITS = {
    "2019-2022（前半段）": ("2019-01-01", "2022-12-31"),
    "2023-2026（后半段）": ("2023-01-01", "2026-12-31"),
}


def main():
    rows = []
    for name, (s, e) in SPLITS.items():
        print(f"\n=== {name} ===")
        r = run(Config(start=s, end=e), verbose=False)
        rows.append({
            "区间": name, "年化": r["cagr"], "基准年化": r["bench_cagr"],
            "超额": r["ann_excess"], "波动": r["vol"], "夏普": r["sharpe"],
            "最大回撤": r["mdd"], "IR": r["ir"],
            "月胜率": r["win_rate_month"], "年换手": r["ann_turnover"],
        })
        print(f"  年化 {r['cagr']*100:.1f}%  基准 {r['bench_cagr']*100:.1f}%  "
              f"超额 {r['ann_excess']*100:.1f}%  夏普 {r['sharpe']}")

    df = pd.DataFrame(rows)
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "split_test.csv", index=False, encoding="utf-8-sig")

    md = ["# 子区间检验\n",
          "| 区间 | 年化 | 基准 | 超额 | 波动 | 夏普 | 最大回撤 | IR | 月胜率 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in df.iterrows():
        md.append(f"| {r['区间']} | {r['年化']*100:.1f}% | {r['基准年化']*100:.1f}% | "
                  f"{r['超额']*100:.1f}% | {r['波动']*100:.1f}% | {r['夏普']:.2f} | "
                  f"{r['最大回撤']*100:.1f}% | {r['IR']:.2f} | {r['月胜率']*100:.0f}% |")
    (OUT / "子区间检验.md").write_text("\n".join(md), encoding="utf-8")
    print("\n已输出 output/子区间检验.md")


if __name__ == "__main__":
    main()
