# -*- coding: utf-8 -*-
"""Floating screen-region overlay — auto OCR + answer beside the frame."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    ROOT = Path(__file__).resolve().parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

import bank_io
from app import (
    APP_ID,
    HOST,
    PORT,
    answer_in_options,
    load_bank,
    normalize_text,
    options_look_reliable,
    question_looks_like_quiz,
    reload_bank,
    resolve_option_text,
    solve_image_bytes,
    warm_up,
)

# Windows tkinter transparent pixel
TRANSPARENT = "#010101"
TOOLBAR_H = 30
BORDER = 3
ANSWER_W = 250
DEFAULT_CAPTURE_W = 420
DEFAULT_CAPTURE_H = 260
SCAN_MS = 1400
MIN_CAPTURE_W = 220
MIN_CAPTURE_H = 120
MAX_CAPTURE_W = 1200
MAX_CAPTURE_H = 720
HANDLE = 16
IMPORT_CONF_MAX = 78
IMPORT_MIN_H = 480
WINDOW_TITLE = "万国觉醒 · 悬浮识题"
MUTEX_NAME = "Local\\RoKQuizOverlaySingleInstance"
_MUTEX_HANDLE = None


def _config_path() -> Path:
    return bank_io.user_data_dir() / "overlay.json"


def _acquire_single_instance() -> bool:
    """Return False if another overlay already holds the mutex."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        _MUTEX_HANDLE = handle
        return not already
    except Exception:
        return True


def focus_existing_overlay() -> bool:
    """Bring an already-running overlay to the front. Returns True if found."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        # Keep above other windows (matches -topmost)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        return True
    except Exception:
        return False


def _load_config() -> dict:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(x: int, y: int, cw: int, ch: int) -> None:
    payload = {
        "x": x,
        "y": y,
        "capture_w": cw,
        "capture_h": ch,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _config_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _server_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")) and data.get("app") == APP_ID
    except Exception:
        return False


def _solve_via_http(png_bytes: bytes) -> dict:
    boundary = f"----RoKOverlay{int(time.time() * 1000)}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="region.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + png_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}/api/solve?region=1",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _solve_local(png_bytes: bytes) -> dict:
    return solve_image_bytes(png_bytes, region_mode=True)


def _add_via_http(question: str, answer: str) -> dict:
    payload = json.dumps({"question": question, "answer": answer}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}/api/questions/add",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _add_local(question: str, answer: str) -> dict:
    result = bank_io.add_user_question(question, answer, source="overlay")
    bank = reload_bank()
    return {**result, "total": len(bank)}


class OverlayApp:
    def __init__(self) -> None:
        import tkinter as tk

        self.tk = tk
        self.cfg = _load_config()
        self.capture_w = int(self.cfg.get("capture_w", DEFAULT_CAPTURE_W))
        self.capture_h = int(self.cfg.get("capture_h", DEFAULT_CAPTURE_H))
        self.paused = True  # wait until frame is on the quiz — avoids OCR'ing Cursor/IDE
        self.scanning = False
        self.last_hash = ""
        self._drag: tuple[int, int] | None = None
        # mode, x0, y0, capture_w, capture_h, root_x, root_y
        self._resize: tuple[str, int, int, int, int, int, int] | None = None
        self._use_http = _server_healthy()
        self._pending_digest = ""
        self._pending_question = ""
        self._matched_question = ""
        self._last_parsed: dict = {}
        self._last_answer = ""
        self._correcting = False
        self._skipped_hashes: set[str] = set()
        self._importing = False
        self._scan_token = 0

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.configure(bg=TRANSPARENT)
        self.root.overrideredirect(True)

        total_w = self.capture_w + ANSWER_W + BORDER * 2
        total_h = max(self.capture_h + TOOLBAR_H + BORDER * 2, IMPORT_MIN_H)
        x = int(self.cfg.get("x", 120))
        y = int(self.cfg.get("y", 120))
        self.root.geometry(f"{total_w}x{total_h}+{x}+{y}")

        self._build_ui()
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<space>", lambda _e: self._toggle_pause())
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.meta_var.set("请把红框对准题目，按空格开始识别")
        self.root.after(300, self._scan_tick)

    def _build_ui(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg=TRANSPARENT)
        outer.pack(fill="both", expand=True)

        toolbar = tk.Frame(outer, bg="#8b3a2a", height=TOOLBAR_H)
        toolbar.pack(fill="x", side="top")
        toolbar.pack_propagate(False)

        title = tk.Label(
            toolbar,
            text="万国觉醒 · 悬浮识题框",
            bg="#8b3a2a",
            fg="#fff8f0",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        title.pack(side="left", padx=8)
        title.bind("<Button-1>", self._start_drag)
        title.bind("<B1-Motion>", self._on_drag)
        title.bind("<ButtonRelease-1>", self._end_drag)
        toolbar.bind("<Button-1>", self._start_drag)
        toolbar.bind("<B1-Motion>", self._on_drag)
        toolbar.bind("<ButtonRelease-1>", self._end_drag)

        self.pause_btn = tk.Button(
            toolbar,
            text="继续",
            command=self._toggle_pause,
            bg="#c45c3e",
            fg="#fff8f0",
            relief="flat",
            font=("Microsoft YaHei UI", 8),
            padx=6,
        )
        self.pause_btn.pack(side="right", padx=(0, 4), pady=4)

        close_btn = tk.Button(
            toolbar,
            text="×",
            command=self.root.destroy,
            bg="#6b2a1f",
            fg="#fff8f0",
            relief="flat",
            font=("Microsoft YaHei UI", 10, "bold"),
            width=2,
        )
        close_btn.pack(side="right", padx=4, pady=4)

        self.correct_btn = tk.Button(
            toolbar,
            text="纠错",
            command=self._on_correct,
            bg="#a05a28",
            fg="#fff8f0",
            relief="flat",
            font=("Microsoft YaHei UI", 8),
            padx=6,
            state="disabled",
        )
        self.correct_btn.pack(side="right", padx=(0, 4), pady=4)

        body = tk.Frame(outer, bg="#f3e6cf")
        body.pack(fill="both", expand=True)

        # Left column: capture (OCR) on top; opaque filler below so empty space
        # is never mistaken for the recognition zone.
        self.left_col = tk.Frame(body, bg="#f3e6cf", width=self.capture_w)
        self.left_col.pack(side="left", fill="y", expand=False)
        self.left_col.pack_propagate(False)

        self.capture_frame = tk.Frame(
            self.left_col, bg=TRANSPARENT, width=self.capture_w, height=self.capture_h
        )
        self.capture_frame.pack(side="top", fill="x", expand=False)
        self.capture_frame.pack_propagate(False)

        self.canvas = tk.Canvas(
            self.capture_frame,
            bg=TRANSPARENT,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._draw_border()

        self.left_filler = tk.Frame(self.left_col, bg="#e8d7b8")
        self.left_filler.pack(side="top", fill="both", expand=True)
        tk.Label(
            self.left_filler,
            text="非识别区\n（只扫上方红框）",
            bg="#e8d7b8",
            fg="#8b6a3a",
            font=("Microsoft YaHei UI", 9),
            justify="center",
        ).place(relx=0.5, rely=0.45, anchor="center")

        answer_wrap = tk.Frame(body, bg="#f3e6cf", width=ANSWER_W)
        answer_wrap.pack(side="right", fill="both", expand=True)
        answer_wrap.pack_propagate(False)
        self.answer_wrap = answer_wrap

        self.title_var = tk.StringVar(value="正确答案")
        tk.Label(
            answer_wrap,
            textvariable=self.title_var,
            bg="#f3e6cf",
            fg="#8b3a2a",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 4))

        self.answer_var = tk.StringVar(value="—")
        self.answer_label = tk.Label(
            answer_wrap,
            textvariable=self.answer_var,
            bg="#f3e6cf",
            fg="#1c1410",
            font=("Microsoft YaHei UI", 13, "bold"),
            wraplength=ANSWER_W - 20,
            justify="left",
            anchor="nw",
        )
        self.answer_label.pack(fill="x", padx=10, pady=(0, 4))

        # Bottom chrome first so it always keeps space.
        size_row = tk.Frame(answer_wrap, bg="#f3e6cf")
        size_row.pack(fill="x", side="bottom", padx=8, pady=(0, 6))
        tk.Button(
            size_row,
            text="−",
            command=lambda: self._nudge_size(-30, -20),
            bg="#e5d2ae",
            fg="#1c1410",
            relief="flat",
            width=2,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.size_var = tk.StringVar(value=self._size_text())
        tk.Label(
            size_row,
            textvariable=self.size_var,
            bg="#f3e6cf",
            fg="#444",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=4)
        tk.Button(
            size_row,
            text="+",
            command=lambda: self._nudge_size(30, 20),
            bg="#e5d2ae",
            fg="#1c1410",
            relief="flat",
            width=2,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self.meta_var = tk.StringVar(value="只扫上方红框 · 拖红框边缘改大小，右侧答案栏不动")
        self.meta_label = tk.Label(
            answer_wrap,
            textvariable=self.meta_var,
            bg="#f3e6cf",
            fg="#666",
            font=("Microsoft YaHei UI", 8),
            wraplength=ANSWER_W - 16,
            justify="left",
        )
        self.meta_label.pack(side="bottom", anchor="w", padx=10, pady=(0, 2))

        # Import actions — packed under the answer title so they never fall below the fold.
        self.action_frame = tk.Frame(answer_wrap, bg="#f3e6cf")
        btn_row = tk.Frame(self.action_frame, bg="#f3e6cf")
        btn_row.pack(fill="x", pady=(0, 4))
        self.import_btn = tk.Button(
            btn_row,
            text="导入题库",
            command=self._on_import,
            bg="#1f6b3a",
            fg="#fff8f0",
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
        )
        self.import_btn.pack(side="left")
        self.skip_btn = tk.Button(
            btn_row,
            text="不导入",
            command=self._on_skip,
            bg="#e5d2ae",
            fg="#1c1410",
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=10,
        )
        self.skip_btn.pack(side="left", padx=(6, 0))
        tk.Label(
            self.action_frame,
            text="已选答案（可手改）",
            bg="#f3e6cf",
            fg="#666",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        self.answer_entry = tk.Entry(
            self.action_frame,
            font=("Microsoft YaHei UI", 10, "bold"),
            bg="#fffaf0",
            fg="#1c1410",
            relief="solid",
            bd=1,
        )
        self.answer_entry.pack(fill="x", pady=(2, 4), ipady=2)

        # Options panel (may clip if short; actions above stay visible)
        self.import_frame = tk.Frame(answer_wrap, bg="#f3e6cf")
        tk.Label(
            self.import_frame,
            text="题目可手改 · 点选项填入答案",
            bg="#f3e6cf",
            fg="#8b3a2a",
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w")
        self.q_var = tk.StringVar(value="")  # kept for compatibility
        self.q_text = tk.Text(
            self.import_frame,
            height=3,
            wrap="word",
            font=("Microsoft YaHei UI", 8),
            bg="#fffaf0",
            fg="#1c1410",
            relief="solid",
            bd=1,
            padx=4,
            pady=2,
        )
        self.q_text.pack(fill="x", pady=(2, 2))
        self.options_frame = tk.Frame(self.import_frame, bg="#f3e6cf")
        self.options_frame.pack(fill="x", pady=(0, 2))

        self._add_resize_handle(
            self.capture_frame,
            "w",
            relx=0.0,
            rely=0.0,
            relwidth=0,
            relheight=1.0,
            width=HANDLE,
            anchor="nw",
            cursor="size_we",
        )
        self._add_resize_handle(
            self.capture_frame,
            "e",
            relx=1.0,
            rely=0.0,
            relwidth=0,
            relheight=1.0,
            width=HANDLE,
            anchor="ne",
            cursor="size_we",
        )
        self._add_resize_handle(
            self.capture_frame,
            "s",
            relx=0.0,
            rely=1.0,
            relwidth=1.0,
            relheight=0,
            height=HANDLE,
            anchor="sw",
            cursor="size_ns",
        )
        self._add_resize_handle(
            self.capture_frame,
            "sw",
            relx=0.0,
            rely=1.0,
            relwidth=0,
            relheight=0,
            width=HANDLE + 4,
            height=HANDLE + 4,
            anchor="sw",
            cursor="size_ne_sw",
        )
        self._add_resize_handle(
            self.capture_frame,
            "se",
            relx=1.0,
            rely=1.0,
            relwidth=0,
            relheight=0,
            width=HANDLE + 4,
            height=HANDLE + 4,
            anchor="se",
            cursor="size_nw_se",
        )
        # Answer panel is independent — no shared SE grip that resizes the OCR frame

    def _size_text(self) -> str:
        return f"{self.capture_w}×{self.capture_h}"

    def _add_resize_handle(self, parent, mode: str, **place_kw) -> None:
        cursor = place_kw.pop("cursor")
        handle = self.tk.Frame(parent, bg="#c45c3e", cursor=cursor)
        handle.place(**place_kw)
        # Visible grip so left/right edges are easy to find on transparent glass
        if mode in {"w", "e"}:
            mark = "▌" if mode == "w" else "▐"
            lab = self.tk.Label(
                handle,
                text=mark,
                bg="#c45c3e",
                fg="#fff8f0",
                font=("Segoe UI", 9, "bold"),
                cursor=cursor,
            )
            lab.place(relx=0.5, rely=0.5, anchor="center")
            lab.bind("<Button-1>", lambda e, m=mode: self._start_resize(e, m))
            lab.bind("<B1-Motion>", self._on_resize)
            lab.bind("<ButtonRelease-1>", self._end_resize)
        handle.bind("<Button-1>", lambda e, m=mode: self._start_resize(e, m))
        handle.bind("<B1-Motion>", self._on_resize)
        handle.bind("<ButtonRelease-1>", self._end_resize)

    def _clamp_size(self, w: int, h: int) -> tuple[int, int]:
        return (
            max(MIN_CAPTURE_W, min(MAX_CAPTURE_W, int(w))),
            max(MIN_CAPTURE_H, min(MAX_CAPTURE_H, int(h))),
        )

    def _apply_size(
        self,
        w: int,
        h: int,
        *,
        x: int | None = None,
        y: int | None = None,
        invalidate: bool = True,
    ) -> None:
        self.capture_w, self.capture_h = self._clamp_size(w, h)
        # Window can be taller than capture (import panel), but OCR stays capture-sized
        total_w = self.capture_w + ANSWER_W + BORDER * 2
        total_h = max(self.capture_h + TOOLBAR_H + BORDER * 2, IMPORT_MIN_H)
        if x is None:
            x = self.root.winfo_x()
        if y is None:
            y = self.root.winfo_y()
        self.root.geometry(f"{total_w}x{total_h}+{x}+{y}")
        self.left_col.configure(width=self.capture_w)
        self.capture_frame.configure(width=self.capture_w, height=self.capture_h)
        self._draw_border()
        self.size_var.set(self._size_text())
        if invalidate:
            self._invalidate_scan()

    def _nudge_size(self, dw: int, dh: int) -> None:
        self._apply_size(self.capture_w + dw, self.capture_h + dh)

    def _draw_border(self) -> None:
        self.canvas.delete("border")
        w = max(self.capture_w, 40)
        h = max(self.capture_h, 40)
        pad = BORDER
        self.canvas.create_rectangle(
            pad,
            pad,
            w - pad,
            h - pad,
            outline="#c45c3e",
            width=BORDER,
            fill=TRANSPARENT,
            tags="border",
        )

    def _capture_bbox(self) -> tuple[int, int, int, int]:
        self.root.update_idletasks()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        left = rx + BORDER
        top = ry + TOOLBAR_H + BORDER
        right = left + self.capture_w - BORDER * 2
        bottom = top + self.capture_h - BORDER * 2
        return left, top, right, bottom

    def _grab_png(self) -> bytes | None:
        """Capture region. Must run on the Tk main thread."""
        from PIL import ImageGrab

        # Match physical pixels on scaled Windows displays
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        self.root.update_idletasks()
        bbox = self._capture_bbox()
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return None
        # Capture without withdraw — full hide/show made the window bounce every scan.
        # Recognition area is transparent; briefly hide only the red border line.
        border_ids = self.canvas.find_withtag("border")
        for item in border_ids:
            self.canvas.itemconfigure(item, state="hidden")
        self.root.update_idletasks()
        try:
            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                img = ImageGrab.grab(bbox=bbox)
        finally:
            for item in border_ids:
                try:
                    self.canvas.itemconfigure(item, state="normal")
                except Exception:
                    pass
            self._draw_border()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _grab_png_from_worker(self) -> bytes | None:
        """Marshal screen grab onto the Tk main thread (tk is not thread-safe)."""
        box: dict = {"png": None, "err": None}
        done = threading.Event()

        def _do() -> None:
            try:
                if not self.root.winfo_exists():
                    return
                box["png"] = self._grab_png()
            except Exception as exc:
                box["err"] = exc
            finally:
                done.set()

        try:
            self.root.after(0, _do)
        except Exception:
            return None
        if not done.wait(timeout=4.0):
            return None
        if box["err"] is not None:
            raise box["err"]
        return box["png"]

    def _persist_geometry(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
            _save_config(
                self.root.winfo_x(),
                self.root.winfo_y(),
                self.capture_w,
                self.capture_h,
            )
        except Exception:
            pass

    def _start_drag(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event) -> None:
        if not self._drag:
            return
        nx = event.x_root - self._drag[0]
        ny = event.y_root - self._drag[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _invalidate_scan(self) -> None:
        """Drop in-flight OCR results after the frame moves or a new session starts."""
        self._scan_token += 1
        self.last_hash = ""

    def _end_drag(self, _event=None) -> None:
        self._drag = None
        self._persist_geometry()
        self._mark_answer_stale()
        self._invalidate_scan()
        # Frame moved → old "不导入" skips no longer apply
        self._skipped_hashes.clear()

    def _start_resize(self, event, mode: str = "se") -> None:
        self._resize = (
            mode,
            event.x_root,
            event.y_root,
            self.capture_w,
            self.capture_h,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )

    def _on_resize(self, event) -> None:
        if not self._resize:
            return
        mode, sx, sy, sw, sh, ox, oy = self._resize
        nw, nh = sw, sh
        nx, ny = ox, oy
        dx = event.x_root - sx
        dy = event.y_root - sy
        if mode in {"e", "se"}:
            nw = sw + dx
        if mode in {"w", "sw"}:
            # Drag left edge: shrink/grow width and shift window so right edge stays put.
            nw = sw - dx
            clamped_w, _ = self._clamp_size(nw, sh)
            # If clamp blocked further shrink/grow, don't keep sliding the window.
            applied_dx = sw - clamped_w
            nx = ox + applied_dx
            nw = clamped_w
        if mode in {"s", "se", "sw"}:
            nh = sh + dy
        self._apply_size(nw, nh, x=nx, y=ny)

    def _end_resize(self, _event=None) -> None:
        self._resize = None
        self._persist_geometry()
        self._mark_answer_stale()
        self._invalidate_scan()
        self._skipped_hashes.clear()

    def _toggle_pause(self) -> None:
        # Import / 纠错 panel owns the session — don't resume underneath it
        # (otherwise _correcting stays True and every later result is ignored).
        if self.paused and (self._correcting or self.import_frame.winfo_ismapped()):
            self.meta_var.set("请先点「导入/保存」或「不导入/取消」，再继续识别")
            return
        self.paused = not self.paused
        self.pause_btn.configure(text="继续" if self.paused else "暂停")
        if self.paused:
            self.meta_var.set("已暂停 · 答案仍是暂停前的，移动红框后请按空格重识")
        else:
            # Force a fresh scan — don't keep previous question's answer
            self._correcting = False
            self._importing = False
            self._invalidate_scan()
            self.answer_var.set("识别中…")
            self._matched_question = ""
            self._last_answer = ""
            self.meta_var.set("自动识别中…")

    def _mark_answer_stale(self, reason: str = "") -> None:
        """Clear frozen answer after the frame moves so it can't be mistaken for a new Q."""
        if not self.paused:
            return
        if self._last_answer or self.answer_var.get() not in {"", "—", "识别中…"}:
            self.answer_var.set("—")
            self._last_answer = ""
            self._matched_question = ""
            tip = "红框已移动 · 按空格重新识别"
            if reason:
                tip = reason
            self.meta_var.set(tip)

    def _clear_options(self) -> None:
        for child in self.options_frame.winfo_children():
            child.destroy()

    def _compact_window_height(self) -> int:
        return max(self.capture_h + TOOLBAR_H + BORDER * 2, 200)

    def _restore_compact_height(self) -> None:
        """Shrink back after import/纠错 panel closes — stops height bouncing."""
        total_w = self.capture_w + ANSWER_W + BORDER * 2
        need = self._compact_window_height()
        try:
            cur_h = int(self.root.winfo_height())
            cur_w = int(self.root.winfo_width())
        except Exception:
            cur_h, cur_w = 0, 0
        if abs(cur_h - need) < 8 and abs(cur_w - total_w) < 8:
            return
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{total_w}x{need}+{x}+{y}")
        self.left_col.configure(width=self.capture_w)
        self.capture_frame.configure(width=self.capture_w, height=self.capture_h)

    def _hide_import_panel(self) -> None:
        self._pending_digest = ""
        self._pending_question = ""
        self._correcting = False
        self._clear_options()
        if self.import_frame.winfo_ismapped():
            self.import_frame.pack_forget()
        if self.action_frame.winfo_ismapped():
            self.action_frame.pack_forget()
        self.title_var.set("正确答案")
        self.answer_entry.delete(0, "end")
        self.q_var.set("")
        try:
            self.q_text.delete("1.0", "end")
        except Exception:
            pass
        self.import_btn.configure(text="导入题库")
        self.skip_btn.configure(text="不导入")
        self._restore_compact_height()

    def _show_import_panel(
        self,
        parsed: dict,
        digest: str,
        *,
        correcting: bool = False,
        prefill: str = "",
    ) -> None:
        question = str(parsed.get("question") or "").strip()
        options = parsed.get("options") or []
        hinted = str(parsed.get("hinted_answer") or "").strip()

        # Keep user-typed/selected answer across OCR refreshes of the same question
        prev_answer = ""
        same_question = False
        if self.action_frame.winfo_ismapped() and self._pending_question:
            prev_answer = self.answer_entry.get().strip()
            same_question = normalize_text(question) == normalize_text(self._pending_question)

        self._pending_digest = digest
        self._pending_question = question
        self._correcting = correcting
        if correcting:
            self.title_var.set("纠错改答案")
            self.import_btn.configure(text="保存更正")
            self.skip_btn.configure(text="取消")
            tip = "点选项或手改后保存"
        else:
            self.title_var.set("题库未收录")
            self.import_btn.configure(text="导入题库")
            self.skip_btn.configure(text="不导入")
            tip = "已填入识别答案，可点选项或手改"

        display_q = question  # full text — user may edit OCR mistakes
        self.q_var.set(display_q)
        self.q_text.delete("1.0", "end")
        if display_q:
            self.q_text.insert("1.0", display_q)

        seed = ""
        # Keep whatever the user already typed/picked for this same question.
        # (Don't let a later green-hint OCR refresh overwrite their choice.)
        if same_question and prev_answer:
            seed = prev_answer
        elif hinted:
            seed = resolve_option_text(hinted, options) or hinted
        if not seed:
            seed = prefill or ""
            if seed and options:
                seed = resolve_option_text(seed, options) or seed
        if not seed and options:
            # Prefer first option that contains digits (e.g. 14根)
            for opt in options:
                text = str(opt.get("text") or "").strip()
                if text and re.search(r"\d", text):
                    seed = text
                    break
            if not seed:
                seed = str(options[0].get("text") or "").strip()

        self.answer_entry.delete(0, "end")
        if seed:
            self.answer_entry.insert(0, seed)
            self.answer_var.set(seed)
        else:
            self.answer_var.set("请选择答案")

        self._clear_options()
        if options:
            for opt in options[:4]:
                key = opt.get("key", "")
                text = str(opt.get("text", "")).strip()
                if not text:
                    continue
                label = f"{key}. {text}" if key else text
                btn = self.tk.Button(
                    self.options_frame,
                    text=label[:28] + ("…" if len(label) > 28 else ""),
                    command=lambda t=text: self._pick_option(t),
                    bg="#fffaf0",
                    fg="#1c1410",
                    relief="solid",
                    bd=1,
                    font=("Microsoft YaHei UI", 8),
                    anchor="w",
                    padx=4,
                    pady=0,
                )
                btn.pack(fill="x", pady=1)
            if not options_look_reliable(options, question):
                tip = "选项数字可能不准，请对照屏幕改后导入"
        elif not correcting:
            tip = "选项识别不准，请手填后导入"
        elif correcting:
            tip = "请手填正确答案后保存"

        # Pause while user confirms import, so rescans don't clear the entry
        if not self.paused:
            self.paused = True
            self.pause_btn.configure(text="继续")

        if not self.action_frame.winfo_ismapped():
            self.action_frame.pack(fill="x", padx=10, pady=(0, 2), after=self.answer_label)
        if not self.import_frame.winfo_ismapped():
            self.import_frame.pack(fill="x", padx=10, pady=(0, 2), after=self.action_frame)
        self.meta_var.set(tip)
        self._ensure_import_height()

    def _ensure_import_height(self) -> None:
        """Grow window so import actions and options stay usable (OCR frame unchanged)."""
        need = max(self.capture_h + TOOLBAR_H + BORDER * 2, IMPORT_MIN_H)
        # Extra room when 4 options are listed
        if len(self.options_frame.winfo_children()) >= 3:
            need = max(need, IMPORT_MIN_H + 40)
        total_w = self.capture_w + ANSWER_W + BORDER * 2
        try:
            if abs(int(self.root.winfo_height()) - need) < 8 and abs(int(self.root.winfo_width()) - total_w) < 8:
                return
        except Exception:
            pass
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{total_w}x{need}+{x}+{y}")
        self.left_col.configure(width=self.capture_w)
        self.capture_frame.configure(width=self.capture_w, height=self.capture_h)
        self.root.update_idletasks()

    def _resume_scanning(self, tip: str = "自动识别中…") -> None:
        """Leave 纠错/导入 UI and keep scanning next questions."""
        self.paused = False
        self.pause_btn.configure(text="暂停")
        self._invalidate_scan()
        self.meta_var.set(tip)

    def _pick_option(self, text: str) -> None:
        self.answer_entry.delete(0, "end")
        self.answer_entry.insert(0, text)
        self.answer_var.set(text)
        if self._correcting:
            self.meta_var.set("已改选 · 点「保存更正」写入题库")
        else:
            self.meta_var.set("已选答案 · 点「导入题库」或「不导入」")

    def _on_correct(self) -> None:
        parsed = dict(self._last_parsed or {})
        if not parsed.get("question") and self._matched_question:
            parsed["question"] = self._matched_question
        if not parsed.get("question"):
            self.meta_var.set("暂无识别题目，无法纠错")
            return
        # Pause while editing so auto-scan won't close the panel
        if not self.paused:
            self.paused = True
            self.pause_btn.configure(text="继续")
        self._show_import_panel(
            parsed,
            self._pending_digest or self.last_hash,
            correcting=True,
            prefill=self._last_answer,
        )

    def _on_skip(self) -> None:
        was_correcting = self._correcting
        if self._pending_digest and not was_correcting:
            self._skipped_hashes.add(self._pending_digest)
        prev_answer = self._last_answer
        self._hide_import_panel()
        if was_correcting:
            # Cancel 纠错 must fully unlock scanning — previously stayed paused
            # and kept IMPORT_MIN_H, so the next Q only bounced the window.
            if prev_answer:
                self.answer_var.set(prev_answer)
            else:
                self.answer_var.set("—")
            self._resume_scanning("已取消纠错 · 继续识别中")
        else:
            self.answer_var.set("已跳过")
            self._resume_scanning("本轮不导入 · 换题后继续识别")

    def _edited_question(self) -> str:
        """Question text from the editable box (falls back to pending OCR stem)."""
        try:
            typed = self.q_text.get("1.0", "end").strip()
        except Exception:
            typed = ""
        if typed:
            return typed
        return (getattr(self, "_pending_question", "") or "").strip()

    def _on_import(self) -> None:
        if self._importing:
            return
        # Always prefer what the user sees/edits in the question box
        question = self._edited_question()
        # Prefer bank matched stem only when correcting AND user didn't edit away from it
        if (
            self._correcting
            and self._matched_question
            and normalize_text(question) == normalize_text(self._pending_question or "")
        ):
            question = self._matched_question.strip()
        answer = self.answer_entry.get().strip()
        if not question:
            self.meta_var.set("题目为空，无法导入")
            return
        if not answer:
            self.meta_var.set("请先选择或填写答案")
            return

        self._pending_question = question
        self._importing = True
        self.import_btn.configure(state="disabled")
        self.skip_btn.configure(state="disabled")
        self.correct_btn.configure(state="disabled")
        self.meta_var.set("正在保存…" if self._correcting else "正在导入…")

        def worker() -> None:
            try:
                if self._use_http:
                    try:
                        result = _add_via_http(question, answer)
                    except Exception:
                        self._use_http = False
                        result = _add_local(question, answer)
                else:
                    result = _add_local(question, answer)
                # Solve path is always local — keep this process's bank cache in sync
                # even when the desktop HTTP API wrote the file.
                try:
                    reload_bank()
                except Exception:
                    pass
                status = result.get("status", "added")
                if self._correcting:
                    msg = "已更正题库"
                else:
                    msg = "已更新题库" if status == "updated" else "已导入题库"
                self.root.after(0, lambda: self._import_done(True, msg, answer))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._import_done(False, str(e), answer))

        threading.Thread(target=worker, daemon=True).start()

    def _import_done(self, ok: bool, message: str, answer: str) -> None:
        self._importing = False
        self.import_btn.configure(state="normal")
        self.skip_btn.configure(state="normal")
        if ok:
            # Remember this frame so we don't instantly re-open import UI,
            # but do NOT blacklist it forever in _skipped_hashes (that blocked later Qs
            # when the screen hash barely changed / user resumed incorrectly).
            done_digest = self._pending_digest
            self._last_answer = answer
            self._correcting = False
            self._hide_import_panel()
            self.title_var.set("正确答案")
            self.answer_var.set(answer)
            self.meta_var.set(message)
            self.correct_btn.configure(state="normal")
            self.paused = False
            self.pause_btn.configure(text="暂停")
            # Suppress re-scan of the same pixels; next different frame will scan
            self.last_hash = done_digest or self.last_hash
            self.meta_var.set(message + " · 换题后自动识别")
        else:
            self.meta_var.set(f"保存失败: {message[:60]}")
            self.correct_btn.configure(state="normal")

    def _scan_tick(self) -> None:
        if self.root.winfo_exists():
            if not self.paused and not self.scanning and not self._importing:
                # Mark busy on the main thread to avoid overlapping workers.
                self.scanning = True
                threading.Thread(target=self._scan_worker, daemon=True).start()
            self.root.after(SCAN_MS, self._scan_tick)

    def _scan_worker(self) -> None:
        token = self._scan_token
        try:
            png = self._grab_png_from_worker()
            if not png:
                return
            if token != self._scan_token:
                return
            digest = hashlib.md5(png).hexdigest()
            if digest == self.last_hash:
                return
            if digest in self._skipped_hashes:
                self.last_hash = digest
                return

            # Always solve locally so code/OCR fixes apply without restarting desktop server.
            result = _solve_local(png)
            if token != self._scan_token:
                return

            self.root.after(0, lambda r=result, d=digest, t=token: self._apply_result(r, d, t))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self.meta_var.set(f"识别失败: {e}"))
        finally:
            self.scanning = False

    def _apply_result(self, result: dict, digest: str = "", token: int | None = None) -> None:
        # Discard OCR that finished after the frame moved / a newer scan started
        if token is not None and token != self._scan_token:
            return
        if digest:
            self.last_hash = digest
        # Don't clobber an active correction / import confirmation session
        if self._importing:
            return
        # Safety: correcting flag without panel would swallow all future answers
        if self._correcting and not self.import_frame.winfo_ismapped():
            self._correcting = False
        if self._correcting:
            return
        if (
            self.import_frame.winfo_ismapped()
            and self._pending_question
            and self.answer_entry.get().strip()
        ):
            new_parsed = result.get("parsed") or {}
            new_q = str(new_parsed.get("question") or "").strip()
            if new_q and normalize_text(new_q) == normalize_text(self._pending_question):
                # Same question: still refresh when OCR options improved (e.g. 根 → 14根)
                if not self._options_better(new_parsed.get("options"), self._last_parsed.get("options")):
                    return
            elif new_q and normalize_text(new_q) != normalize_text(self._pending_question):
                # New question while user is mid-edit — don't overwrite typed answer
                return

        if result.get("error"):
            self._hide_import_panel()
            self.answer_var.set("—")
            self.meta_var.set(str(result["error"])[:80])
            self.correct_btn.configure(state="disabled")
            return

        answer = result.get("answer")
        conf = result.get("confidence") or 0
        needs_import = bool(result.get("needs_import"))
        parsed = result.get("parsed") or {}
        self._last_parsed = parsed
        self._matched_question = str(result.get("matched_question") or "").strip()
        self._last_answer = str(answer or "").strip()

        can_correct = bool(parsed.get("question") or self._matched_question)
        self.correct_btn.configure(state="normal" if can_correct else "disabled")

        if answer and not needs_import:
            self._hide_import_panel()
            self.answer_var.set(str(answer))
            mq = result.get("matched_question") or ""
            ms = result.get("timing_ms") or {}
            total = ms.get("total")
            tail = f" · {total}ms" if total else ""
            self.meta_var.set(f"匹配度 {conf}% · {mq[:36]}{tail}")
            return

        if needs_import and parsed.get("question") and question_looks_like_quiz(
            str(parsed.get("question") or "")
        ):
            self._show_import_panel(parsed, digest, prefill=str(answer or ""))
            return

        self._hide_import_panel()
        # Non-quiz OCR (Cursor/IDE chrome) — don't pretend it's a missing bank entry
        ocr_preview = str(result.get("ocr_text") or "")[:80]
        if ocr_preview and not question_looks_like_quiz(str(parsed.get("question") or "")):
            self.answer_var.set("—")
            self.meta_var.set("未对准题目（可能扫到了别的窗口）· 调整红框后重试")
            self.correct_btn.configure(state="disabled")
            return
        self.answer_var.set("未找到")
        self.meta_var.set("题库暂无此题 · 可点「纠错」手选导入")

    @staticmethod
    def _options_better(new_opts, old_opts) -> bool:
        """True when new OCR options look more complete than what is already shown."""
        def score(opts) -> tuple[int, int]:
            texts = [str(o.get("text") or "").strip() for o in (opts or []) if o]
            texts = [t for t in texts if t]
            digit_n = sum(1 for t in texts if re.search(r"\d", t))
            chars = sum(len(re.sub(r"\s+", "", t)) for t in texts)
            return digit_n, chars

        return score(new_opts) > score(old_opts)

    def run(self) -> None:
        def _on_destroy(event) -> None:
            # Destroy bubbles for every child; only persist when the root dies.
            if event.widget is self.root:
                self._persist_geometry()

        self.root.bind("<Destroy>", _on_destroy)
        self.root.mainloop()


def main() -> None:
    if not _acquire_single_instance():
        if focus_existing_overlay():
            print("悬浮识题框已在运行，已切换到前台", file=sys.stderr)
            raise SystemExit(0)
        print("悬浮识题框已在运行", file=sys.stderr)
        raise SystemExit(0)

    try:
        count = len(load_bank())
    except Exception as exc:
        print(f"题库加载失败: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if not _server_healthy():
        warm_up()
    print(f"悬浮识题框 · 已加载 {count} 题")
    OverlayApp().run()


if __name__ == "__main__":
    main()