# -*- coding: utf-8 -*-
"""
选股助手 · 启动器

双击后：
    1. 在控制台打印启动状态（能看见在干什么，出错也能看见）
    2. 起本地服务 127.0.0.1:8770
    3. 自动用系统默认浏览器打开界面
    4. 窗口保持打开；关掉窗口 = 停止服务

命令行：
    StockPicker.exe                默认
    StockPicker.exe --port 8771    换端口
    StockPicker.exe --no-browser   只起服务
    StockPicker.exe --quiet        不等待按键（脚本用）
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

QUIET = "--quiet" in sys.argv
NO_BROWSER = "--no-browser" in sys.argv

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _open_browser(url: str) -> bool:
    """打开界面。

    冻结成 exe 后 webbrowser.open() 经常"返回 True 但什么也没发生"，
    所以按可靠性从高到低逐个尝试。
    """
    # 1) cmd start：Windows 上最标准的"用默认程序打开"
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], close_fds=True,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        _log("  已请求系统打开默认浏览器 (cmd start)")
        return True
    except Exception:  # noqa: BLE001
        pass

    # 2) 直接拉起已知浏览器
    for p in EDGE_PATHS + CHROME_PATHS:
        if os.path.exists(p):
            try:
                subprocess.Popen([p, url], close_fds=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                _log(f"  已用 {Path(p).name} 打开界面")
                return True
            except Exception:  # noqa: BLE001
                pass

    # 3) ShellExecute
    try:
        os.startfile(url)          # noqa: S606
        _log("  已用系统默认程序打开界面")
        return True
    except Exception:  # noqa: BLE001
        pass

    # 4) 最后兜底
    try:
        ok = webbrowser.open(url)
        _log(f"  webbrowser.open -> {ok}")
        return bool(ok)
    except Exception:  # noqa: BLE001
        _log("  打开浏览器失败：" + traceback.format_exc(limit=1))
        return False


def _copy_to_clipboard(text: str) -> bool:
    """把地址复制到剪贴板，浏览器没弹出来时 Ctrl+V 就能打开。"""
    try:
        p = subprocess.run(["clip"], input=text.encode("utf-16-le"),
                           shell=False, timeout=5)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _fix_console():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None and hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_fix_console()


def _log(msg: str):
    print(msg, flush=True)
    try:
        from app import config
        with open(config.RUNTIME / "startup.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _pause(msg="\n按回车键退出…"):
    if QUIET:
        return
    try:
        input(msg)
    except Exception:  # noqa: BLE001
        pass


def _alive(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _parse_port() -> int:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            return int(args[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    return 8770


def main():
    port = _parse_port()
    url = f"http://127.0.0.1:{port}"

    if _alive(port):
        _log(f"服务已经在运行：{url}")
        if not NO_BROWSER:
            _open_browser(url)
        _pause()
        return

    from app import config, web

    _log("=" * 58)
    _log("  A股选股助手")
    _log(f"  版本 {config.APP_VERSION}   构建 {time.strftime('%Y-%m-%d', time.localtime())}")
    _log("=" * 58)
    _log(f"  程序目录 : {config.ROOT}")
    if not (config.PANEL / "close.parquet").exists():
        _log(f"  [警告] 缺少行情数据 {config.PANEL / 'close.parquet'}")

    _log("  正在启动本地服务…")
    t = threading.Thread(target=web.serve, args=("127.0.0.1", port, True),
                         daemon=True)
    t.start()

    ok = False
    for _ in range(60):
        if _alive(port):
            ok = True
            break
        time.sleep(0.5)

    if not ok:
        _log("  [失败] 服务没有起来（端口被占用或被杀毒软件拦截）")
        _pause()
        return

    _log(f"  服务已就绪 : {url}")
    _log("")
    _log("  浏览器会自动打开；如果没打开，手动复制上面的地址。")
    _log("  保持本窗口开着 = 服务在运行；关掉本窗口 = 停止服务。")
    _log("=" * 58)

    if not NO_BROWSER:
        time.sleep(0.8)
        _open_browser(url)
        if _copy_to_clipboard(url):
            _log("")
            _log("  >>> 如果浏览器没有自动弹出：地址已复制到剪贴板，")
            _log("      直接 Ctrl+V 粘贴到浏览器地址栏就能打开。")
            _log(f"      地址：{url}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _log("  收到中断，退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        print(err, flush=True)
        _log("fatal:\n" + err)
        _pause()
