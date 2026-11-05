# -*- coding: utf-8 -*-
"""把「目标组合」和「当前持仓」做差，生成可执行的调仓单。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, pricing


def _prices(signal_date: str, codes: list[str]) -> dict:
    close = pd.read_parquet(config.PANEL / "close.parquet")
    d = pd.Timestamp(signal_date)
    row = close.loc[:d].iloc[-1] if d not in close.index else close.loc[d]
    return {c: (float(row[c]) if c in row.index and row[c] == row[c] else None)
            for c in codes}


def _sigmas(signal_date: str, codes: list[str]) -> dict:
    """调仓单里被清掉的票不在信号里，波动率得自己算一遍。"""
    try:
        close = pd.read_parquet(config.PANEL / "close.parquet")
        d = pd.Timestamp(signal_date)
        s = pricing.sigma_at(close, d if d in close.index else close.index.max())
        return {c: float(s.get(c, float("nan"))) for c in codes}
    except Exception:  # noqa: BLE001
        return {}


def compute(signal: dict, holdings: dict, settings: dict | None = None) -> dict:
    s = settings or config.load_settings()
    comm = float(s.get("commission", 0.00025))
    stamp = float(s.get("stamp", 0.0005))
    slip = float(s.get("slippage", 0.001))

    picks = {x["code"]: x for x in signal["picks"]}
    pos = {k: float(v) for k, v in holdings.get("positions", {}).items() if float(v) > 0}
    cash = float(holdings.get("cash", 0.0))

    all_codes = list(set(picks) | set(pos))
    px_map = _prices(signal["date"], all_codes)
    for c, x in picks.items():                 # 优先用信号里的价格
        if x.get("price"):
            px_map[c] = float(x["price"])
    sig_map = _sigmas(signal["date"], all_codes)

    try:
        uni = pd.read_parquet(config.DATA / "universe.parquet")
        name_map = uni.set_index("code")["name"].astype(str).to_dict()
    except Exception:  # noqa: BLE001
        name_map = {}

    exposure = float(signal.get("exposure", 1.0) or 1.0)
    mv = sum(sh * px_map[c] for c, sh in pos.items() if px_map.get(c))
    total = cash + mv
    if total <= 0:
        total = float(signal.get("capital", 1_000_000))
        cash = total

    rows = []
    for c in set(picks) | set(pos):
        px = px_map.get(c)
        sh = pos.get(c, 0.0)
        w = float(picks[c]["weight"]) if c in picks else 0.0
        tgt_sh = 0
        if px and w > 0:
            tgt_sh = int(round(total * exposure * w / px / 100) * 100)
        delta = tgt_sh - sh
        if abs(delta) < 1:
            action = "HOLD"
        elif delta > 0:
            action = "BUY"
        else:
            action = "SELL"
        amount = abs(delta) * (px or 0)
        fee = amount * (comm + slip + (stamp if action == "SELL" else 0))
        # 参考成交价：买用「挂单价/可追上限」，卖用「挂单价/最低接受」
        band = (picks.get(c, {}).get("pricing") or {})
        if not band and px:
            band = pricing.band(px, sig_map.get(c), 0.0)
        if action == "BUY":
            post_px, edge_px = band.get("buy_limit"), band.get("buy_cap")
        elif action == "SELL":
            post_px, edge_px = band.get("sell_limit"), band.get("sell_floor")
        else:
            post_px, edge_px = None, None
        rows.append({
            "code": c,
            "name": picks.get(c, {}).get("name") or name_map.get(c, ""),
            "industry": picks.get(c, {}).get("industry", ""),
            "price": round(px, 2) if px else None,
            "post_price": post_px,
            "edge_price": edge_px,
            "target": band.get("target"),
            "take_profit": band.get("take_profit"),
            "stop_loss": band.get("stop"),
            "current_shares": int(sh),
            "target_shares": int(tgt_sh),
            "delta": int(delta),
            "action": action,
            "amount": round(amount, 0),
            "fee": round(fee, 0),
            "weight": round(w, 4),
        })

    rows.sort(key=lambda r: ({"SELL": 0, "BUY": 1, "HOLD": 2}[r["action"]],
                             -r["amount"]))
    sells = [r for r in rows if r["action"] == "SELL"]
    buys = [r for r in rows if r["action"] == "BUY"]

    cash_after_sell = cash + sum(r["amount"] * (1 - comm - slip - stamp) for r in sells)
    need = sum(r["amount"] * (1 + comm + slip) for r in buys)
    scale = 1.0
    if need > cash_after_sell > 0:
        scale = cash_after_sell / need
        for r in buys:
            new_sh = int((r["target_shares"]) * scale / 100) * 100
            d = new_sh - r["current_shares"]
            if d <= 0:
                r["action"] = "HOLD"
                r["target_shares"] = r["current_shares"]
                r["delta"] = 0
                r["amount"] = 0.0
                r["fee"] = 0.0
            else:
                r["target_shares"] = new_sh
                r["delta"] = d
                r["amount"] = round(d * (r["price"] or 0), 0)
                r["fee"] = round(r["amount"] * (comm + slip), 0)
        buys = [r for r in rows if r["action"] == "BUY"]
        rows.sort(key=lambda r: ({"SELL": 0, "BUY": 1, "HOLD": 2}[r["action"]],
                                 -r["amount"]))

    buy_amt = sum(r["amount"] for r in buys)
    sell_amt = sum(r["amount"] for r in sells)
    return {
        "date": signal["date"],
        "total_assets": round(total, 0),
        "market_value": round(mv, 0),
        "cash": round(cash, 0),
        "exposure": round(exposure, 3),
        "cash_scale": round(scale, 3),
        "n_buy": len(buys),
        "n_sell": len(sells),
        "n_hold": len([r for r in rows if r["action"] == "HOLD"]),
        "buy_amount": round(buy_amt, 0),
        "sell_amount": round(sell_amt, 0),
        "est_cost": round(sum(r["fee"] for r in rows), 0),
        "turnover": round((buy_amt + sell_amt) / (2 * total), 4) if total else 0,
        "orders": rows,
    }
