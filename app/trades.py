# -*- coding: utf-8 -*-
"""
成交记录：把「调仓单」的点按变成真实持仓。

记一笔 = 改持仓 + 扣/加现金 + 落一条流水；
撤销 = 反向操作并删除流水。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from . import config, storage

TRADES_FILE = config.RUNTIME / "trades.json"


def _read() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    try:
        rows = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def _write(rows: list[dict]):
    TRADES_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def list_trades(limit: int = 200) -> list[dict]:
    rows = _read()
    return list(reversed(rows[-limit:]))


def _fee(amount: float, side: str) -> float:
    s = config.load_settings()
    rate = float(s.get("commission", 0.00025)) + float(s.get("slippage", 0.001))
    if side == "SELL":
        rate += float(s.get("stamp", 0.0005))
    return round(amount * rate, 2)


def record(code: str, name: str, side: str, shares, price,
           fee: float | None = None) -> dict:
    code = str(code).strip()
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("方向必须是 BUY 或 SELL")
    try:
        shares = int(round(float(shares)))
        price = float(price)
    except (TypeError, ValueError):
        raise ValueError("股数或价格格式不对")
    if shares <= 0:
        raise ValueError("股数必须大于 0")
    if price <= 0:
        raise ValueError("价格必须大于 0")

    amount = round(shares * price, 2)
    fee = _fee(amount, side) if fee is None else round(float(fee), 2)

    h = storage.load_holdings()
    cash = float(h["cash"])
    pos = dict(h["positions"])

    if side == "BUY":
        if cash - amount - fee < -1e-6:
            raise ValueError(f"现金不足：需要 {amount + fee:,.0f}，可用 {cash:,.0f}")
        cash -= amount + fee
        pos[code] = float(pos.get(code, 0)) + shares
    else:
        have = float(pos.get(code, 0))
        if shares > have + 1e-9:
            raise ValueError(f"卖出 {shares} 股超过持仓 {have:.0f} 股")
        cash += amount - fee
        pos[code] = have - shares
        if pos[code] <= 1e-9:
            pos.pop(code, None)

    storage.save_holdings(cash, pos)

    t = {
        "id": uuid.uuid4().hex[:12],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code, "name": name or "",
        "side": side, "shares": shares,
        "price": round(price, 3),
        "amount": amount, "fee": fee,
    }
    rows = _read()
    rows.append(t)
    _write(rows)
    storage.log(f"trade {side} {code} {shares}股@{price} fee={fee}")
    return t


def undo(tid: str) -> dict:
    rows = _read()
    t = next((x for x in rows if x.get("id") == tid), None)
    if t is None:
        raise ValueError("找不到这条成交记录")

    h = storage.load_holdings()
    cash = float(h["cash"])
    pos = dict(h["positions"])
    amt = float(t["amount"])
    fee = float(t["fee"])
    shares = int(t["shares"])
    code = t["code"]

    if t["side"] == "BUY":
        have = float(pos.get(code, 0))
        if shares > have + 1e-9:
            raise ValueError(f"当前持仓 {have:.0f} 股，不足以撤销这笔买入")
        pos[code] = have - shares
        if pos[code] <= 1e-9:
            pos.pop(code, None)
        cash += amt + fee
    else:
        pos[code] = float(pos.get(code, 0)) + shares
        cash -= amt - fee

    storage.save_holdings(cash, pos)
    _write([x for x in rows if x.get("id") != tid])
    storage.log(f"trade undo {tid} {code}")
    return {"ok": True, "id": tid}
