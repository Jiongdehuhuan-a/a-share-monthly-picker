# -*- coding: utf-8 -*-
"""全局配置与路径。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 冻结成 exe 后：
#   ROOT   = exe 所在目录（数据、输出、配置放这里，可写）
#   APP    = PyInstaller 解压出的 _internal 目录（只读的代码与静态资源）
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(sys.executable).resolve().parent
    _BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
    APP = _BUNDLE / "app"
    SRC = ROOT / "src"
else:
    ROOT = Path(__file__).resolve().parents[1]
    APP = ROOT / "app"
    SRC = ROOT / "src"

DATA = ROOT / "data"
PANEL = DATA / "panel"
OUTPUT = ROOT / "output"
RUNTIME = DATA / "runtime"
STATIC = APP / "static"
SETTINGS_FILE = RUNTIME / "settings.json"
HOLDINGS_FILE = RUNTIME / "holdings.json"
SIGNAL_FILE = RUNTIME / "signal.json"
SIGNAL_HISTORY = RUNTIME / "signal_history.json"
LOG_FILE = RUNTIME / "app.log"

# 前端版本号：改前端时必须同步 app/static/app.js 里的 APP_V，
# 旧页面轮询时发现版本不一致会自动刷新。
APP_VERSION = "2026-11-03.3"

for p in (RUNTIME, STATIC):
    p.mkdir(parents=True, exist_ok=True)

# 让 app 能 import src/backtest.py
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_SETTINGS = {
    "top_n": 50,
    "buffer_ratio": 1.5,
    "max_stock_w": 0.05,
    "max_ind_w": 0.25,
    "min_list_days": 250,
    "min_amt20": 5e7,
    "min_price": 2.0,
    # 调优后的默认权重（2026-11 网格实验，见 output/策略优化.md）：
    # 六个核心因子等权 + 短期反转 0.3 的相对权重（≈ 各 15.9% / 反转 4.8%）
    "weights": {
        "ep": 0.1587, "bp": 0.1587, "momentum": 0.1587,
        "lowvol": 0.1587, "turnover": 0.1587, "size": 0.1587,
        "reversal": 0.0476,
    },
    "quality_screen": True,     # 质量门槛：剔除 ROE<0 或毛利率<0 的股票
    "quality_tilt": 0.0,        # 质量倾斜（0~0.3，实测越大收益越低，默认关闭）
    "news_enabled": True,       # 是否用新闻情绪
    "news_tilt": 0.15,          # 新闻情绪倾斜强度
    "news_veto": True,          # 硬利空（立案/退市/造假）一票否决
    "capital": 1_000_000,
    "commission": 0.00025,
    "stamp": 0.0005,
    "slippage": 0.001,
    "auto_update_hour": 17,     # 每个交易日 17:00 后自动更新数据
    "auto_signal": True,        # 每月最后一个交易日自动生成信号
}

WEIGHT_LABELS = {
    "ep": "盈利收益率 EP",
    "bp": "账面市值比 BP",
    "momentum": "中期动量(6-1)",
    "lowvol": "低波动",
    "turnover": "低换手",
    "size": "小市值",
    "reversal": "短期反转(20日)",
    "roe": "净资产收益率 ROE",
    "gm": "销售毛利率",
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            saved = {}
        cfg = {**DEFAULT_SETTINGS, **saved}
        cfg["weights"] = {**DEFAULT_SETTINGS["weights"],
                          **saved.get("weights", {})}
        return cfg
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def _clamp(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:                     # NaN
        return default
    return max(lo, min(hi, x))


def save_settings(s: dict) -> dict:
    """保存并夹紧参数，避免前端空输入把策略打崩。"""
    cfg = {**DEFAULT_SETTINGS, **(s or {})}
    w = cfg.get("weights") or {}
    tot = sum(max(0.0, float(v)) for v in w.values() if _isnum(v)) or 1.0
    cfg["weights"] = {k: round(max(0.0, float(v)) / tot, 4)
                      for k, v in w.items() if _isnum(v)}
    if not cfg["weights"]:
        cfg["weights"] = dict(DEFAULT_SETTINGS["weights"])
    cfg["top_n"] = int(_clamp(cfg.get("top_n"), 10, 200, 50))
    cfg["max_ind_w"] = _clamp(cfg.get("max_ind_w"), 0.05, 1.0, 0.25)
    cfg["max_stock_w"] = _clamp(cfg.get("max_stock_w"), 0.01, 1.0, 0.05)
    cfg["min_amt20"] = _clamp(cfg.get("min_amt20"), 1e6, 1e10, 5e7)
    cfg["min_price"] = _clamp(cfg.get("min_price"), 0.5, 100, 2.0)
    cfg["capital"] = _clamp(cfg.get("capital"), 10000, 1e10, 1_000_000)
    cfg["quality_tilt"] = _clamp(cfg.get("quality_tilt"), 0.0, 0.5, 0.0)
    cfg["news_tilt"] = _clamp(cfg.get("news_tilt"), 0.0, 0.5, 0.15)
    cfg["buffer_ratio"] = _clamp(cfg.get("buffer_ratio"), 1.0, 3.0, 1.5)
    SETTINGS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    try:
        from . import storage
        storage.log(f"settings saved: top_n={cfg['top_n']} "
                    f"ind={cfg['max_ind_w']} cap={cfg['capital']} "
                    f"qtilt={cfg['quality_tilt']} ntilt={cfg['news_tilt']}")
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _isnum(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
