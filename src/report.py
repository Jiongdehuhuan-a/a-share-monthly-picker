# -*- coding: utf-8 -*-
"""根据回测输出生成图表与 Markdown 报告。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 130
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.25
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False

C_STRAT, C_BM = "#c0392b", "#2c3e50"


def fig_performance(nav: pd.DataFrame, yearly: dict, path: Path):
    s, b = nav["nav"], nav["bench"]
    fig, ax = plt.subplots(2, 2, figsize=(13, 8.5))
    ax = ax.ravel()

    ax[0].plot(s.index, s / s.iloc[0], color=C_STRAT, lw=1.6, label="策略")
    ax[0].plot(b.index, b / b.iloc[0], color=C_BM, lw=1.2, label="中证800")
    ax[0].set_yscale("log")
    ax[0].set_title("累计净值（对数坐标）")
    ax[0].legend(frameon=False, loc="upper left")

    dd = s / s.cummax() - 1
    db = b / b.cummax() - 1
    ax[1].fill_between(dd.index, dd * 100, 0, color=C_STRAT, alpha=.35, label="策略")
    ax[1].plot(db.index, db * 100, color=C_BM, lw=1, label="中证800")
    ax[1].set_title("回撤（%）")
    ax[1].legend(frameon=False, loc="lower left")

    y = pd.DataFrame(yearly).T.sort_index()
    x = np.arange(len(y))
    ax[2].bar(x - 0.2, y["策略"] * 100, 0.4, color=C_STRAT, label="策略")
    ax[2].bar(x + 0.2, y["基准"] * 100, 0.4, color=C_BM, label="中证800")
    ax[2].set_xticks(x)
    ax[2].set_xticklabels([str(int(i)) for i in y.index], rotation=0)
    ax[2].axhline(0, color="k", lw=.8)
    ax[2].set_title("分年度收益（%）")
    ax[2].legend(frameon=False)

    ex = (y["策略"] - y["基准"]) * 100
    ax[3].bar(x, ex, 0.55, color=np.where(ex >= 0, C_STRAT, "#7f8c8d"))
    ax[3].set_xticks(x)
    ax[3].set_xticklabels([str(int(i)) for i in y.index], rotation=0)
    ax[3].axhline(0, color="k", lw=.8)
    ax[3].set_title("年度超额收益（%）")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def fig_ic(ic: pd.DataFrame, path: Path):
    ic = ic.dropna(subset=["ic"]).copy()
    ic["date"] = pd.to_datetime(ic["date"])
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.2),
                           gridspec_kw={"width_ratios": [2.4, 1]})
    ax[0].bar(ic["date"], ic["ic"], width=20, color=np.where(ic["ic"] > 0, C_STRAT, "#7f8c8d"))
    roll = ic.set_index("date")["ic"].rolling(12).mean()
    ax[0].plot(roll.index, roll.values, color="#e67e22", lw=1.8, label="12期滚动均值")
    ax[0].axhline(0, color="k", lw=.8)
    ax[0].set_title("综合因子 RankIC（每期）")
    ax[0].legend(frameon=False)

    cols = [c for c in ic.columns if c.startswith("ic_")]
    if cols:
        means = ic[cols].mean().sort_values()
        labels = [c.replace("ic_", "") for c in means.index]
        ax[1].barh(labels, means.values, color="#34495e")
        ax[1].axvline(0, color="k", lw=.8)
        ax[1].set_title("各因子平均 RankIC")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _pct(x, nd=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x*100:.{nd}f}%"


def write_report(tag: str, metrics: dict, ic: pd.DataFrame, turnover: pd.DataFrame,
                 portfolio: dict | None) -> str:
    y = pd.DataFrame(metrics["yearly"]).T.sort_index()
    lines = []
    A = lines.append
    A(f"# A股多因子选股策略 · 回测报告\n")
    A(f"> 回测区间 **{metrics['start']} ~ {metrics['end']}**"
      f"（{metrics['years']} 年） ｜ 基准 **中证800** ｜ 标签 `{tag}`\n")
    A("## 一、核心指标\n")
    A("| 指标 | 策略 | 中证800 |")
    A("|---|---:|---:|")
    A(f"| 累计收益 | {_pct(metrics['total_return'])} | {_pct(metrics['benchmark_total'])} |")
    A(f"| 年化收益 | **{_pct(metrics['cagr'])}** | {_pct(metrics['bench_cagr'])} |")
    A(f"| 年化超额 | **{_pct(metrics['ann_excess'])}** | — |")
    A(f"| 年化波动 | {_pct(metrics['vol'])} | — |")
    A(f"| 夏普比率 | {metrics['sharpe']:.2f} | — |")
    A(f"| 最大回撤 | {_pct(metrics['mdd'])} | — |")
    A(f"| 卡玛比率 | {metrics['calmar']:.2f} | — |")
    A(f"| 信息比率 IR | {metrics['ir']:.2f} | — |")
    A(f"| Beta | {metrics['beta']:.2f} | 1.00 |")
    A(f"| 月度胜率 | {_pct(metrics['win_rate_month'])} | — |")
    A(f"| 月度跑赢基准概率 | {_pct(metrics['win_rate_excess'])} | — |")
    A(f"| 年化单边换手 | {metrics['ann_turnover']*100:.0f}% | — |")
    A("")
    A("![绩效](fig_performance.png)\n")
    A("## 二、分年度表现\n")
    A("| 年度 | 策略 | 中证800 | 超额 |")
    A("|---|---:|---:|---:|")
    for k, row in y.iterrows():
        A(f"| {int(k)} | {_pct(row['策略'])} | {_pct(row['基准'])} | {_pct(row['超额'])} |")
    A("")
    A("## 三、因子有效性（RankIC）\n")
    valid = ic.dropna(subset=["ic"])
    icir = valid["ic"].mean() / valid["ic"].std() if len(valid) > 2 else np.nan
    A(f"- 综合因子平均 RankIC **{valid['ic'].mean():.4f}**，"
      f"ICIR **{icir:.2f}**，IC>0 占比 **{(valid['ic']>0).mean()*100:.1f}%**")
    A(f"- 样本期数 {len(valid)}（月频）\n")
    cols = [c for c in ic.columns if c.startswith("ic_")]
    if cols:
        A("| 因子 | 平均IC | ICIR | 胜率 |")
        A("|---|---:|---:|---:|")
        for c in cols:
            v = ic[c].dropna()
            if len(v) < 5:
                continue
            A(f"| {c.replace('ic_','')} | {v.mean():.4f} | "
              f"{v.mean()/v.std():.2f} | {(v>0).mean()*100:.0f}% |")
    A("")
    A("![IC](fig_ic.png)\n")
    if portfolio:
        A(f"## 四、最新一期持仓（{portfolio['date']}，股票仓位 {portfolio['exposure']*100:.0f}%）\n")
        A("| 代码 | 名称 | 行业 | 权重 |")
        A("|---|---|---|---:|")
        items = sorted(portfolio["weights"].items(), key=lambda kv: -kv[1])
        for c, w in items[:30]:
            A(f"| {c} | {portfolio['names'].get(c,'')} | "
              f"{portfolio['industry'].get(c,'')} | {w*100:.1f}% |")
        if len(items) > 30:
            A(f"\n> 共 {len(items)} 只，仅列前 30。")
        A("")
    A("## 五、说明\n")
    A("- 收益已扣除佣金 2.5bp（双边）、印花税 5bp（卖出）、单边滑点 10bp。")
    A("- 信号在月末收盘生成，次一交易日开盘成交；涨停不可买、跌停不可卖。")
    A("- 股票池为当前仍在交易的 A 股，**存在幸存者偏差**，实盘表现通常低于回测。")
    A("- 回测不含分红再投资的独立建模，价格采用前复权，已隐含分红调整。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="N50_RS")
    args = ap.parse_args()
    tag = args.tag

    with open(OUT / f"metrics_{tag}.json", encoding="utf-8") as f:
        metrics = json.load(f)
    nav = pd.read_csv(OUT / f"nav_{tag}.csv", index_col=0, parse_dates=True)
    ic = pd.read_csv(OUT / f"ic_{tag}.csv")
    to = pd.read_csv(OUT / f"turnover_{tag}.csv")

    fig_performance(nav, metrics["yearly"], OUT / "fig_performance.png")
    fig_ic(ic, OUT / "fig_ic.png")
    md = write_report(tag, metrics, ic, to, metrics.get("last_portfolio"))
    (OUT / "回测报告.md").write_text(md, encoding="utf-8")
    print("已生成:", OUT / "回测报告.md", OUT / "fig_performance.png", OUT / "fig_ic.png")


if __name__ == "__main__":
    main()
