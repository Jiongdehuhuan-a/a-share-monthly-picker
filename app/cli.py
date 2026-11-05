# -*- coding: utf-8 -*-
"""命令行入口。

    python -m app.cli serve [--port 8770] [--schedule]
    python -m app.cli update
    python -m app.cli signal
    python -m app.cli orders
    python -m app.cli status
"""
from __future__ import annotations

import argparse
import json

from . import config, datafeed, orders as orders_mod, signals, storage, web


def cmd_status(_):
    st = datafeed.panel_status()
    sig = storage.load_signal()
    h = storage.load_holdings()
    print(json.dumps({
        "数据": st,
        "信号": ({"日期": sig["date"], "只数": sig["n_picks"]} if sig else None),
        "持仓": {"只数": len(h["positions"]), "现金": h["cash"]},
    }, ensure_ascii=False, indent=2))


def cmd_update(_):
    def cb(p, m=""):
        print(f"\r{int(p*100):3d}%  {m:<40}", end="", flush=True)
    res = datafeed.update(progress_cb=cb)
    print("\n完成:", json.dumps(res, ensure_ascii=False))


def cmd_signal(_):
    h = storage.load_holdings()
    sig = signals.generate(config.load_settings(),
                           prev_codes=list(h["positions"].keys()),
                           holdings=h,
                           progress_cb=lambda p, m="": None)
    print(f"信号日 {sig['date']}  可选 {sig['n_eligible']} 只  选出 {sig['n_picks']} 只\n")
    for x in sig["picks"][:20]:
        print(f"  {x['code']}  {x['name']:<8} {x['industry']:<8} "
              f"权重 {x['weight']*100:5.2f}%  参考价 {x['price']}  "
              f"建议 {x['shares']} 股")
    print(f"  … 共 {sig['n_picks']} 只，明细见界面或 data/runtime/signal.json")


def cmd_orders(_):
    sig = storage.load_signal()
    if not sig:
        print("还没有信号，先运行: python -m app.cli signal")
        return
    res = orders_mod.compute(sig, storage.load_holdings(), config.load_settings())
    print(f"总资产 {res['total_assets']:,.0f}  市值 {res['market_value']:,.0f}  "
          f"现金 {res['cash']:,.0f}")
    print(f"买入 {res['n_buy']} 笔 {res['buy_amount']:,.0f} 元 ｜ "
          f"卖出 {res['n_sell']} 笔 {res['sell_amount']:,.0f} 元 ｜ "
          f"预估费用 {res['est_cost']:,.0f} 元")
    for r in res["orders"]:
        if r["action"] == "HOLD":
            continue
        print(f"  {r['action']:<4} {r['code']} {r['name']:<8} "
              f"{r['delta']:+7d} 股 @ {r['price']}  ≈ {r['amount']:,.0f} 元")


def main():
    ap = argparse.ArgumentParser(prog="stock-picker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="启动本地界面")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--schedule", action="store_true", help="开启每交易日自动更新")
    s.set_defaults(fn=lambda a: web.serve(a.host, a.port, a.schedule))
    sub.add_parser("status", help="查看状态").set_defaults(fn=cmd_status)
    sub.add_parser("update", help="增量更新数据").set_defaults(fn=cmd_update)
    sub.add_parser("signal", help="生成选股信号").set_defaults(fn=cmd_signal)
    sub.add_parser("orders", help="生成调仓单").set_defaults(fn=cmd_orders)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
