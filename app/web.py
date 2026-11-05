# -*- coding: utf-8 -*-
"""本机 Web 服务：选股助手界面 + JSON API。"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import (config, datafeed, equity, news, orders as orders_mod,
               signals, storage, trades as trades_mod)

HERE = Path(__file__).resolve().parent

# 页面心跳：桌面模式靠它判断"窗口被关掉了"
PING = {"t": 0.0, "n": 0}


def mark_ping():
    PING["t"] = time.time()
    PING["n"] += 1


def last_ping() -> float:
    return PING["t"]


# ------------------------------------------------------------ 后台任务
def _run_update():
    storage.JOB.start("update")
    try:
        res = datafeed.update(
            progress_cb=lambda p, m="": storage.JOB.update(p, m), workers=10)
        storage.JOB.done(f"数据已更新至 {res.get('last_date')}")
    except Exception as e:  # noqa: BLE001
        storage.log("update failed: " + traceback.format_exc())
        storage.JOB.fail(str(e))


def _run_signal():
    storage.JOB.start("signal")
    try:
        h = storage.load_holdings()
        storage.JOB.update(0.1, "计算因子…")
        sig = signals.generate(config.load_settings(),
                               prev_codes=list(h["positions"].keys()),
                               holdings=h,
                               progress_cb=lambda p, m="": storage.JOB.update(p, m))
        storage.JOB.done(f"{sig['date']} 选出 {sig['n_picks']} 只")
    except Exception as e:  # noqa: BLE001
        storage.log("signal failed: " + traceback.format_exc())
        storage.JOB.fail(str(e))


def _start(name: str, fn) -> bool:
    if storage.JOB.status == "running":
        return False
    threading.Thread(target=fn, name=name, daemon=True).start()
    return True


# ------------------------------------------------------------ 数据组装
def status_payload() -> dict:
    st = datafeed.panel_status()
    h = storage.load_holdings()
    sig = storage.load_signal()
    mk = None
    try:
        if news.LIVE.get("updated"):
            mk = {"index": news.LIVE["index"], "updated": news.LIVE["updated"],
                  "live": True}
        else:
            nf = config.RUNTIME / "news_cache.json"
            if nf.exists():
                m2 = json.loads(nf.read_text(encoding="utf-8")).get("market")
                if m2:
                    mk = {"index": m2.get("index"), "updated": m2.get("updated"),
                          "live": False}
    except Exception:  # noqa: BLE001
        mk = None
    return {
        "data": st,
        "job": storage.JOB.as_dict(),
        "holdings": {"n": len(h["positions"]), "cash": h["cash"],
                     "updated": h["updated"]},
        "signal": ({"date": sig["date"], "n": sig["n_picks"],
                    "generated": sig["generated"]} if sig else None),
        "market_news": ({"index": mk.get("index"), "built": mk.get("updated"),
                         "live": mk.get("live", False)} if mk else None),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def performance_payload() -> dict:
    tag = "N50_noRS"
    f = config.OUTPUT / f"metrics_{tag}.json"
    if not f.exists():
        return {"available": False}
    m = json.loads(f.read_text(encoding="utf-8"))
    return {"available": True, "tag": tag, **m}


def live_news_payload() -> dict:
    """实时快讯：只读内存缓冲，不打网络。"""
    names = []
    try:
        h = storage.load_holdings()
        if h["positions"]:
            import pandas as pd
            uni = pd.read_parquet(config.DATA / "universe.parquet")
            m = uni.set_index("code")["name"].astype(str).to_dict()
            names = [m.get(c, "") for c in h["positions"]]
    except Exception:  # noqa: BLE001
        names = []
    return news.live_payload(names)


def news_payload() -> dict:
    f = config.RUNTIME / "news_cache.json"
    if not f.exists():
        return {"market": None, "stocks": [], "built": None}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"market": None, "stocks": [], "built": None}
    try:
        import pandas as pd
        uni = pd.read_parquet(config.DATA / "universe.parquet")
        name_map = uni.set_index("code")["name"].astype(str).to_dict()
    except Exception:  # noqa: BLE001
        name_map = {}
    stocks = []
    for code, v in (d.get("stocks") or {}).items():
        z = float(v.get("z", 0.0))
        stocks.append({
            "code": code, "name": name_map.get(code, ""),
            "score": v.get("score", 0.0), "z": z,
            "n": v.get("n", 0), "pos": v.get("pos", 0), "neg": v.get("neg", 0),
            "hard": v.get("hard", False),
            "label": "利好" if z > 0.6 else "利空" if z < -0.6 else "中性",
            "items": v.get("items", []),
        })
    stocks.sort(key=lambda x: abs(x["z"]), reverse=True)
    return {"market": d.get("market"), "built": d.get("built"), "stocks": stocks}


# ------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "StockPicker/1.0"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    # ---------- 工具
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---------- GET
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(config.STATIC / "index.html")
            if p.startswith("/static/"):
                return self._file(config.STATIC / p[len("/static/"):])
            if p.startswith("/img/"):
                return self._file(config.OUTPUT / p[len("/img/"):])
            if p == "/api/status":
                return self._json(status_payload())
            if p == "/api/job":
                return self._json(storage.JOB.as_dict())
            if p == "/api/ping":
                mark_ping()
                return self._json({"ok": True, "n": PING["n"]})
            if p == "/api/signal":
                s = storage.load_signal()
                return self._json(s or {"empty": True})
            if p == "/api/holdings":
                return self._json(storage.load_holdings())
            if p == "/api/orders":
                s = storage.load_signal()
                if not s:
                    return self._json({"error": "还没有信号，请先生成"}, 400)
                return self._json(orders_mod.compute(
                    s, storage.load_holdings(), config.load_settings()))
            if p == "/api/trades":
                return self._json({"trades": trades_mod.list_trades()})
            if p == "/api/equity":
                return self._json(equity.payload())
            if p == "/api/settings":
                return self._json(config.load_settings())
            if p == "/api/performance":
                return self._json(performance_payload())
            if p == "/api/news":
                return self._json(news_payload())
            if p == "/api/news/live":
                return self._json(live_news_payload())
            if p == "/api/log":
                if config.LOG_FILE.exists():
                    txt = config.LOG_FILE.read_text(encoding="utf-8")[-4000:]
                else:
                    txt = ""
                return self._send(200, txt.encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            storage.log("GET error: " + traceback.format_exc())
            return self._json({"error": str(e)}, 500)

    # ---------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/update":
                ok = _start("update", _run_update)
                return self._json({"started": ok}, 200 if ok else 409)
            if p == "/api/signal":
                ok = _start("signal", _run_signal)
                return self._json({"started": ok}, 200 if ok else 409)
            if p == "/api/holdings":
                b = self._body()
                return self._json(storage.save_holdings(
                    float(b.get("cash", 0)), b.get("positions") or {}))
            if p == "/api/settings":
                return self._json(config.save_settings(self._body()))
            if p == "/api/holdings/from_signal":
                s = storage.load_signal()
                if not s:
                    return self._json({"error": "没有信号"}, 400)
                pos = {x["code"]: x["shares"] for x in s["picks"] if x["shares"] > 0}
                return self._json(storage.save_holdings(0.0, pos))
            if p == "/api/trade":
                b = self._body()
                try:
                    t = trades_mod.record(b.get("code", ""), b.get("name", ""),
                                          b.get("side", ""), b.get("shares", 0),
                                          b.get("price", 0), b.get("fee"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "trade": t,
                                   "holdings": storage.load_holdings()})
            if p == "/api/trade/undo":
                b = self._body()
                try:
                    trades_mod.undo(b.get("id", ""))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True,
                                   "holdings": storage.load_holdings()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            storage.log("POST error: " + traceback.format_exc())
            return self._json({"error": str(e)}, 500)

    def _file(self, path: Path):
        path = path.resolve()
        if not path.exists() or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)


# ------------------------------------------------------------ 定时任务
def _last_trading_day_of_month(d: pd.Timestamp) -> bool:
    import pandas as pd  # noqa: F401
    nxt = d + pd.Timedelta(days=1)
    while nxt.month == d.month:
        if nxt.weekday() < 5:
            return False
        nxt += pd.Timedelta(days=1)
    return True


def _auto_windows(now: datetime, s: dict) -> list[str]:
    """当前时刻该自动更新数据的窗口。

    每个窗口每天只跑一次：
      am  11:35 上午收盘后（拿到半日最新价）
      pm  15:05 下午收盘后（拿到最终收盘价，覆盖盘中快照）
      eve 晚上兜底（默认 17 点后，settings.auto_update_hour 可改）
    """
    t = now.time()
    out = []
    if dt_time(11, 35) <= t < dt_time(13, 0):
        out.append("am")
    if t >= dt_time(15, 5):
        out.append("pm")
    if now.hour >= int(s.get("auto_update_hour", 17)):
        out.append("eve")
    return out


def _needs_update(window: str, st: dict, now: datetime) -> bool:
    """该窗口是否还需要真的去拉数据（避免每次开软件都白跑 4 分钟）。

    - 面板里没有今天：任何窗口都要更新；
    - am / eve：已经有今天的数据就不用再拉；
    - pm：只有“今天的数据是 15:00 前拿到的”（即盘中快照）才需要重拉收盘价。
    """
    last = st.get("last_date")
    today = now.date().isoformat()
    if not last or last < today:
        return True
    if window != "pm":
        return False
    try:
        mtime = datetime.fromtimestamp(
            (config.PANEL / "close.parquet").stat().st_mtime)
    except OSError:
        return False
    return mtime < now.replace(hour=15, minute=0, second=0, microsecond=0)


def scheduler_loop(stop_flag: dict, interval: int = 60):
    import pandas as pd
    last_auto = 0.0          # 上次自动更新的时间戳，防止重复触发
    done: dict[str, set] = {}   # {"2026-11-04": {"am", "pm"}}
    while not stop_flag.get("stop"):
        try:
            s = config.load_settings()
            now = datetime.now()
            st = datafeed.panel_status()
            last = st.get("last_date")
            today = now.date().isoformat()
            if len(done) > 7:
                done.clear()
            ran = done.setdefault(today, set())
            cooled = (time.time() - last_auto) > 300
            if (st.get("ready") and now.weekday() < 5 and cooled
                    and storage.JOB.status != "running"):
                for w in _auto_windows(now, s):
                    if w in ran or not _needs_update(w, st, now):
                        ran.add(w)
                        continue
                    storage.log(f"scheduler: auto update ({w})")
                    if _start("update", _run_update):
                        ran.add(w)
                        last_auto = time.time()
                    break
            if (s.get("auto_signal") and storage.JOB.status != "running"
                    and st.get("ready") and last == today
                    and _last_trading_day_of_month(pd.Timestamp(today))):
                sig = storage.load_signal()
                if not sig or sig.get("date") != today:
                    storage.log("scheduler: auto signal")
                    _start("signal", _run_signal)
        except Exception:  # noqa: BLE001
            storage.log("scheduler error: " + traceback.format_exc())
        for _ in range(interval):
            if stop_flag.get("stop"):
                break
            time.sleep(1)


_HTTPD = None


def shutdown():
    """外部（桌面启动器）调用，停掉服务。"""
    global _HTTPD
    if _HTTPD is not None:
        try:
            _HTTPD.shutdown()
        except Exception:  # noqa: BLE001
            pass


def serve(host: str = "127.0.0.1", port: int = 8770, schedule: bool = False):
    global _HTTPD
    httpd = ThreadingHTTPServer((host, port), Handler)
    _HTTPD = httpd
    stop = {"stop": False}
    news.start_live_loop(30, stop)
    if schedule:
        threading.Thread(target=scheduler_loop, args=(stop,),
                         daemon=True).start()
        storage.log(f"auto-scheduler enabled")
    try:
        print(f"选股助手已启动: http://{host}:{port}")
    except Exception:  # noqa: BLE001
        pass
    storage.log(f"server start http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop["stop"] = True
        httpd.shutdown()
