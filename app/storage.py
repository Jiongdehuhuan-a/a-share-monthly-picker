# -*- coding: utf-8 -*-
"""持仓库、信号历史、运行日志。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import config


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------ 持仓
def load_holdings() -> dict:
    h = _read(config.HOLDINGS_FILE, {})
    return {
        "cash": float(h.get("cash", 0.0)),
        "positions": {str(k): float(v) for k, v in (h.get("positions") or {}).items()},
        "updated": h.get("updated", ""),
    }


def save_holdings(cash: float, positions: dict) -> dict:
    obj = {
        "cash": round(float(cash), 2),
        "positions": {str(k): round(float(v), 4)
                      for k, v in positions.items() if float(v) > 0},
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write(config.HOLDINGS_FILE, obj)
    return obj


# ------------------------------------------------------------ 信号
def save_signal(sig: dict):
    _write(config.SIGNAL_FILE, sig)
    hist = _read(config.SIGNAL_HISTORY, [])
    hist = [h for h in hist if h.get("date") != sig.get("date")]
    hist.append({"date": sig.get("date"), "generated": sig.get("generated"),
                 "n": len(sig.get("picks", []))})
    hist = sorted(hist, key=lambda x: x.get("date") or "")[-60:]
    _write(config.SIGNAL_HISTORY, hist)


def load_signal() -> dict | None:
    s = _read(config.SIGNAL_FILE, None)
    return s if s else None


# ------------------------------------------------------------ 日志
def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    with open(config.LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


class Job:
    """单例后台任务：记录状态供前端轮询。"""

    def __init__(self):
        self.name = ""
        self.status = "idle"      # idle | running | done | error
        self.progress = 0.0
        self.message = ""
        self.started = 0.0
        self.finished = 0.0

    def as_dict(self):
        return {
            "name": self.name, "status": self.status,
            "progress": round(self.progress, 3), "message": self.message,
            "elapsed": round((self.finished or time.time()) - self.started, 1)
            if self.started else 0,
        }

    def start(self, name: str):
        self.name = name
        self.status = "running"
        self.progress = 0.0
        self.message = "开始…"
        self.started = time.time()
        self.finished = 0.0

    def update(self, progress: float, message: str = ""):
        self.progress = max(0.0, min(1.0, float(progress)))
        if message:
            self.message = message

    def done(self, message: str = "完成"):
        self.status = "done"
        self.progress = 1.0
        self.message = message
        self.finished = time.time()

    def fail(self, message: str):
        self.status = "error"
        self.message = message
        self.finished = time.time()


JOB = Job()
