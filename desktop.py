# -*- coding: utf-8 -*-
"""Desktop window entry — native app window, no CMD console."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path


def _silence_console() -> None:
    """Hide console only for windowed launches (pythonw / packaged EXE)."""
    if sys.platform != "win32":
        return
    if "--console" in sys.argv or os.environ.get("ROK_CONSOLE") == "1":
        return
    frozen = getattr(sys, "frozen", False)
    is_pythonw = sys.executable.lower().endswith("pythonw.exe")
    if not frozen and not is_pythonw:
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    except Exception:
        pass


_silence_console()

if getattr(sys, "frozen", False):
    ROOT = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    ROOT = Path(__file__).resolve().parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

from app import APP_ID, HOST, PORT, app, load_bank, warm_up  # noqa: E402

WINDOW_BG = "#f3e6cf"
WINDOW_TITLE = "万国觉醒题库"
WINDOW_WIDTH = 980
WINDOW_HEIGHT = 860
MUTEX_NAME = "Local\\RoKQuizBankSingleInstance"


def _log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "万国觉醒题库"
    base.mkdir(parents=True, exist_ok=True)
    return base / "runtime.log"


def _write_log(msg: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _msg(text: str, title: str = WINDOW_TITLE, error: bool = True) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
    except Exception:
        pass


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _our_server_healthy(port: int = PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=1.2) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")) and data.get("app") == APP_ID
    except Exception:
        return False


def _wait_our_server(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _our_server_healthy():
            return True
        time.sleep(0.2)
    return False


def _run_flask() -> None:
    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").disabled = True
    try:
        # Preload OCR/bank in background so first paste is fast
        threading.Thread(target=warm_up, daemon=True).start()
        app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
    except Exception:
        _write_log("flask crashed:\n" + traceback.format_exc())


def _acquire_single_instance() -> bool:
    """Return False if another instance already holds the mutex."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        # Keep handle alive for process lifetime
        global _MUTEX_HANDLE
        _MUTEX_HANDLE = handle
        return not already
    except Exception:
        return True


_MUTEX_HANDLE = None


def _focus_existing_window() -> None:
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _webview2_available() -> bool:
    """Edge WebView2 Runtime is required by pywebview on Windows."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        for root, path in keys:
            try:
                with winreg.OpenKey(root, path) as key:
                    pv, _ = winreg.QueryValueEx(key, "pv")
                    if pv and pv != "0.0.0.0":
                        return True
            except OSError:
                continue
    except Exception:
        pass
    # Fallback: msedgewebview2.exe often exists with Edge
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft"
        / "EdgeWebView"
        / "Application"
        / "msedgewebview2.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft"
        / "EdgeWebView"
        / "Application"
        / "msedgewebview2.exe",
    ]
    return any(p.is_file() for p in candidates)


def main() -> None:
    _write_log("start")

    if not _acquire_single_instance():
        if _our_server_healthy():
            _focus_existing_window()
            _write_log("second instance -> focus existing")
            return
        _msg("程序已在运行，或 8765 端口被占用。请先关闭已打开的窗口后再试。")
        return

    try:
        count = len(load_bank())
        _write_log(f"bank loaded: {count}")
    except Exception as exc:
        _write_log("bank load failed:\n" + traceback.format_exc())
        _msg(f"题库加载失败：{exc}")
        raise SystemExit(1)

    if not _webview2_available():
        _msg(
            "缺少 Microsoft Edge WebView2 运行库，无法打开软件窗口。\n\n"
            "这是 Windows 桌面程序的系统组件（不是 Python）。\n"
            "Win10/Win11 通常已自带；若缺失请安装：\n"
            "Edge WebView2 运行时（Evergreen Bootstrapper）\n"
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
        )
        raise SystemExit(1)

    url = f"http://{HOST}:{PORT}"

    if _port_open(HOST, PORT):
        if _our_server_healthy():
            _write_log("reuse existing our server")
        else:
            _msg(
                f"端口 {PORT} 已被其他程序占用，无法启动。\n"
                "请关闭占用该端口的程序后重试。"
            )
            raise SystemExit(1)
    else:
        threading.Thread(target=_run_flask, daemon=True).start()
        if not _wait_our_server():
            _write_log("server wait timeout")
            _msg(f"本地服务启动失败（端口 {PORT}）。请查看日志：\n{_log_path()}")
            raise SystemExit(1)

    try:
        import webview
    except ImportError:
        _msg("缺少桌面窗口组件 pywebview。")
        raise SystemExit(1)

    icon_path = None
    for candidate in (
        ROOT / "assets" / "app.ico",
        Path(__file__).resolve().parent / "assets" / "app.ico",
    ):
        if candidate.is_file():
            icon_path = str(candidate)
            break

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(760, 600),
        background_color=WINDOW_BG,
        text_select=True,
        confirm_close=False,
    )

    def _on_start() -> None:
        if not icon_path:
            return
        time.sleep(0.6)
        try:
            import ctypes

            user32 = ctypes.windll.user32
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            WM_SETICON = 0x0080
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if not hwnd:
                return
            hicon_big = user32.LoadImageW(None, icon_path, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
            hicon_small = user32.LoadImageW(None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
        except Exception:
            _write_log("set icon failed:\n" + traceback.format_exc())

    try:
        webview.start(debug=False, func=_on_start)
    except Exception:
        _write_log("webview start failed:\n" + traceback.format_exc())
        _msg(
            "软件窗口启动失败。\n"
            "若提示缺少 WebView2，请先安装 Microsoft Edge WebView2 Runtime。\n"
            f"日志：{_log_path()}"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _write_log("fatal:\n" + traceback.format_exc())
        _msg(f"程序异常退出，日志：\n{_log_path()}")
        raise
