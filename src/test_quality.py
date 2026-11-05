# -*- coding: utf-8 -*-
"""对照试验：质量因子加进去到底有没有用、该怎么加。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest import Config, run

OUT = Path(__file__).resolve().parent.parent / "output"

SIX = {"ep": 1/6, "bp": 1/6, "momentum": 1/6,
       "lowvol": 1/6, "turnover": 1/6, "size": 1/6}
# 6 个主因子各 15%，质量因子合计 10%（会稀释主因子）
LIGHT = {"ep": .15, "bp": .15, "momentum": .15,
         "lowvol": .15, "turnover": .15, "size": .15,
         "roe": .05, "gm": .05}

CASES = {
    "① 六因子（旧版基线）": Config(weights=SIX),
    "② 八因子等权（稀释）": Config(),
    "③ 六因子 + 质量门槛": Config(weights=SIX, quality_screen=True),
    "④ 六因子 + 质量轻权重5%×2": Config(weights=LIGHT),
    "⑤ 六因子 + 质量倾斜0.10": Config(weights=SIX, quality_tilt=0.10),
    "⑥ 六因子 + 质量倾斜0.20": Config(weights=SIX, quality_tilt=0.20),
    "⑦ 六因子 + 质量倾斜0.30": Config(weights=SIX, quality_tilt=0.30),
    "⑧ 质量门槛 + 倾斜0.20": Config(weights=SIX, quality_screen=True,
                                   quality_tilt=0.20),
}


def main():
    rows = []
    for name, cfg in CASES.items():
        print(f"\n=== {name} ===")
        r = run(cfg, verbose=False)
        rows.append({
            "方案": name, "年化": r["cagr"], "超额": r["ann_excess"],
            "波动": r["vol"], "夏普": r["sharpe"], "最大回撤": r["mdd"],
            "卡玛": r["calmar"], "IR": r["ir"], "年换手": r["ann_turnover"],
        })
        print(f"  年化 {r['cagr']*100:.1f}%  超额 {r['ann_excess']*100:.1f}%  "
              f"回撤 {r['mdd']*100:.1f}%  夏普 {r['sharpe']}")
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "quality_test.csv", index=False, encoding="utf-8-sig")
    md = ["# 质量因子对照试验\n",
          "| 方案 | 年化 | 超额 | 波动 | 夏普 | 最大回撤 | 卡玛 | IR | 年换手 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in df.iterrows():
        md.append(f"| {r['方案']} | {r['年化']*100:.1f}% | {r['超额']*100:.1f}% | "
                  f"{r['波动']*100:.1f}% | {r['夏普']:.2f} | {r['最大回撤']*100:.1f}% | "
                  f"{r['卡玛']:.2f} | {r['IR']:.2f} | {r['年换手']*100:.0f}% |")
    (OUT / "质量因子试验.md").write_text("\n".join(md), encoding="utf-8")
    print("\n" + df.to_string(index=False))


if __name__ == "__main__":
    main()
