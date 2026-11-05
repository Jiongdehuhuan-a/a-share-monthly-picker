# -*- coding: utf-8 -*-
"""
稳健性检验：跑多组参数/因子组合，输出对比表。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest import Config, run

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

EQ = {"ep": 1/8, "bp": 1/8, "momentum": 1/8, "lowvol": 1/8,
      "turnover": 1/8, "size": 1/8, "roe": 1/8, "gm": 1/8}
HAND = {"ep": 0.15, "bp": 0.15, "momentum": 0.25,
        "lowvol": 0.20, "turnover": 0.15, "size": 0.10}

VARIANTS: dict[str, Config] = {
    "A 最终版(六因子+质量门槛)": Config(),
    "B 关掉质量门槛": Config(quality_screen=False),
    "C 手调权重+择时40%": Config(weights=HAND, risk_switch=True),
    "D N30": Config(top_n=30),
    "E N80": Config(top_n=80),
    "F 手调权重": Config(weights=HAND),
    "G 去掉动量": Config(weights={"ep": .2, "bp": .2, "lowvol": .3,
                                  "turnover": .2, "size": .1}),
    "H 去掉价值": Config(weights={"momentum": .35, "lowvol": .3,
                                  "turnover": .2, "size": .15}),
    "I 长样本2018起": Config(start="2017-01-01"),
    "J 八因子等权(含ROE/毛利)": Config(weights=EQ),
}


def main():
    rows = []
    for name, cfg in VARIANTS.items():
        print(f"\n=== {name} ===")
        try:
            r = run(cfg, verbose=False)
        except Exception as e:  # noqa: BLE001
            print("  失败:", e)
            continue
        rows.append({
            "变体": name,
            "区间": f"{r['start'][:4]}~{r['end'][:4]}",
            "年化": r["cagr"], "基准年化": r["bench_cagr"],
            "年化超额": r["ann_excess"], "波动": r["vol"],
            "夏普": r["sharpe"], "最大回撤": r["mdd"],
            "卡玛": r["calmar"], "IR": r["ir"],
            "月胜率": r["win_rate_month"],
            "年换手": r["ann_turnover"],
        })
        print(f"  年化 {r['cagr']*100:.1f}%  超额 {r['ann_excess']*100:.1f}%  "
              f"回撤 {r['mdd']*100:.1f}%  夏普 {r['sharpe']}")

    df = pd.DataFrame(rows)
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "variants.csv", index=False, encoding="utf-8-sig")

    md = ["# 稳健性检验\n",
          "| 变体 | 区间 | 年化 | 超额 | 波动 | 夏普 | 最大回撤 | 卡玛 | IR | 月胜率 | 年换手 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in df.iterrows():
        md.append(f"| {r['变体']} | {r['区间']} | {r['年化']*100:.1f}% | "
                  f"{r['年化超额']*100:.1f}% | {r['波动']*100:.1f}% | "
                  f"{r['夏普']:.2f} | {r['最大回撤']*100:.1f}% | "
                  f"{r['卡玛']:.2f} | {r['IR']:.2f} | {r['月胜率']*100:.0f}% | "
                  f"{r['年换手']*100:.0f}% |")
    (OUT / "稳健性检验.md").write_text("\n".join(md), encoding="utf-8")
    print("\n已输出 output/variants.csv 与 output/稳健性检验.md")


if __name__ == "__main__":
    main()
