# -*- coding: utf-8 -*-
"""
每日资产快照与收益计算。

口径：
    总资产 = 现金 + Σ(持股数 × 当日收盘价)
    今日盈亏 = 今日总资产 - 上一个有记录交易日的总资产
    累计盈亏 = 今日总资产 - 第一次记录的总资产

注意：手动改「可用现金」（出入金）也会体现在盈亏里，目前不做现金流拆分。
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from . import config, storage

EQUITY_FILE = config.RUNTIME / "equity.json"


def _load() -> list[dict]:
    if not EQUITY_FILE.exists():
        return []
    try:
        rows = json.loads(EQUITY_FILE.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(rows: list[dict]):
    rows = rows[-400:]          # 只留最近 400 个交易日
    EQUITY_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def _close() -> pd.DataFrame | None:
    f = config.PANEL / "close.parquet"
    if not f.exists():
        return None
    return pd.read_parquet(f)


def _latest_close_date(close: pd.DataFrame) -> pd.Timestamp | None:
    from .datafeed import _cutoff
    idx = close.index[close.index < _cutoff()]
    return idx.max() if len(idx) else None


def _prices(close: pd.DataFrame, codes: list[str],
            date: pd.Timestamp) -> dict:
    if not codes or date not in close.index:
        return {}
    row = close.loc[date]
    return {c: float(row[c]) for c in codes
            if c in row.index and row[c] == row[c]}


def snapshot(force: bool = False) -> dict:
    """把今天的总资产写进 equity.json（同一天只保留最后一条）。"""
    h = storage.load_holdings()
    pos = h["positions"]
    rows = _load()

    close = _close()
    if close is None:
        return {"rows": rows, "summary": _summary(rows), "date": None}
    date = _latest_close_date(close)
    if date is None:
        return {"rows": rows, "summary": _summary(rows), "date": None}

    px = _prices(close, list(pos), date)
    mv = sum(float(sh) * px.get(c, 0.0) for c, sh in pos.items())
    missing = [c for c in pos if c not in px]
    total = round(float(h["cash"]) + mv, 2)
    day = str(date.date())

    rec = {
        "date": day,
        "total": total,
        "cash": round(float(h["cash"]), 2),
        "market_value": round(mv, 2),
        "positions": len(pos),
        "missing_price": missing,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if rows and rows[-1].get("date") == day:
        rows[-1] = rec
    else:
        rows.append(rec)
    _save(rows)
    return {"rows": rows, "summary": _summary(rows), "date": day}


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0, "today_pnl": 0, "today_pct": 0,
                "total_pnl": 0, "total_pct": 0, "base": 0, "days": 0}
    cur = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    base = rows[0]
    today_pnl = round(cur["total"] - prev["total"], 2) if prev else 0.0
    today_pct = (today_pnl / prev["total"]) if prev and prev["total"] else 0.0
    total_pnl = round(cur["total"] - base["total"], 2)
    total_pct = (total_pnl / base["total"]) if base["total"] else 0.0
    return {
        "date": cur["date"],
        "total": cur["total"],
        "cash": cur["cash"],
        "market_value": cur["market_value"],
        "today_pnl": today_pnl,
        "today_pct": round(today_pct, 6),
        "total_pnl": total_pnl,
        "total_pct": round(total_pct, 6),
        "base": base["total"],
        "base_date": base["date"],
        "days": len(rows),
    }


def payload() -> dict:
    """给前端：先刷新今天，再返回汇总和最近 30 天。"""
    snap = snapshot()
    rows = snap["rows"]
    return {"summary": snap["summary"], "rows": rows[-30:], "date": snap["date"]}
