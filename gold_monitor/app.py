"""A native Windows floating gold-price widget, with no runtime dependencies."""

from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser

from .prices import PriceClient, PriceSnapshot
from .settings import Settings, fit_position

TITLE = "Gold Monitor · 黄金现货"
BG = "#111820"
PANEL = "#19232e"
LINE = "#293744"
GOLD = "#f2cb79"
WHITE = "#edf2f7"
MUTED = "#91a1b2"
GREEN = "#69d5b0"
RED = "#ee8991"


def data_directory() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return base / ".state"


def monitor_workareas(root: tk.Tk) -> list[tuple[int, int, int, int]]:
    rectangles: list[tuple[int, int, int, int]] = []
    if sys.platform == "win32":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]

        @callback_type
        def collect(handle, _dc, _rect, _data):
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                area = info.rcWork
                rectangles.append((area.left, area.top, area.right, area.bottom))
            return True

        user32.EnumDisplayMonitors(None, None, collect, 0)
    return rectangles or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]


def age_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def show_in_taskbar(root: tk.Tk):
    """Keep a frameless widget reachable through Windows taskbar / Alt+Tab."""
    if sys.platform != "win32":
        return
    user = ctypes.windll.user32
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    root.update_idletasks()
    handle = user.GetParent(root.winfo_id())
    style = user.GetWindowLongPtrW(handle, -20)
    user.SetWindowLongPtrW(handle, -20, (style & ~0x80) | 0x40000)


class GoldMonitor:
    WIDTH = 374
    HEIGHT = 268
    COMPACT_HEIGHT = 126

    def __init__(self, root: tk.Tk, state_dir: Path | None = None,
                 client: PriceClient | None = None):
        self.root = root
        self.state_dir = state_dir or data_directory()
        self.settings_path = self.state_dir / "settings.json"
        self.settings = Settings.load(self.settings_path)
        self.client = client or PriceClient(timeout=5, source=self.settings.source)
        self.docked = False
        self.ticker = None
        self.pending_source = None
        self.restore_event = None
        self.snapshot: PriceSnapshot | None = None
        self.history: deque[tuple[datetime, Decimal]] = deque(maxlen=120)
        self.session_open: Decimal | None = None
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self.closed = False
        self.next_refresh = 0.0
        self.last_request = float("-inf")
        self.last_error = ""
        self.worker_error = False
        self.price_direction = 0
        self.price_flash_until = 0.0
        self.notice = ""
        self.notice_until = 0.0
        self.drag_origin = None
        self.buttons: dict[str, tuple[int, int, int, int]] = {}
        self.about_window: tk.Toplevel | None = None
        self.context_menu = None
        self.poll_job = None
        self.tick_job = None
        self.refresh_job = None
        self.flash_job = None
        self.dock_job = None
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.root.title(TITLE)
        self.root.configure(bg=LINE)
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", self.settings.topmost)
        self.root.attributes("-alpha", self.settings.opacity)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "assets" / "gold.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=1, highlightbackground=LINE, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.canvas.bind("<Motion>", self._hover)
        self.root.bind("<F5>", lambda _e: self.refresh(manual=True))
        self.root.bind("<Control-p>", lambda _e: self.toggle_pin())
        self.root.bind("<Control-m>", lambda _e: self.toggle_compact())
        self.root.bind("<Control-h>", lambda _e: self.minimize_to_taskbar())
        self.root.bind("<Unmap>", self._on_unmap, add="+")
        self.root.bind("<Alt-F4>", lambda _e: self.close())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._resize(initial=True)
        self._draw()
        self.refresh_job = self.root.after(80, self.refresh)
        self.poll_job = self.root.after(120, self._poll)
        self.tick_job = self.root.after(1000, self._tick)

    @property
    def minimum_interval(self):
        return getattr(self.client, "minimum_interval_seconds", 1)

    def _price_color(self):
        if time.monotonic() < self.price_flash_until:
            return GREEN if self.price_direction > 0 else RED
        return GOLD

    def _update_ticker(self, now):
        if not self.ticker:
            return
        value = self.snapshot.usd_per_oz if self.snapshot else None
        cny = self.snapshot.cny_per_gram if self.snapshot else None
        status, _ = self._status(now)
        arrow = "↑ " if self.price_direction > 0 else "↓ " if self.price_direction < 0 else ""
        if time.monotonic() >= self.price_flash_until:
            arrow = ""
        primary = f"{arrow}${value:,.2f} /oz" if value is not None else "$ — — /oz"
        secondary = f"≈ ¥{cny:,.2f}/克" if cny is not None else "USD/盎司 · 汇率待更新"
        if status in ("连接异常", "连接失败", "报价延迟"):
            secondary = status + " · 上次报价" if value else status
        elif self._fx_old(now):
            secondary = "汇率过期 · " + secondary
        self.ticker.update_price(primary, secondary, color=self._price_color(), status="" if status in secondary else status)

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def _text(self, x, y, text, size=11, color=WHITE, anchor="w", weight="normal", family="Microsoft YaHei UI"):
        return self.canvas.create_text(self.px(x), self.px(y), text=text,
                                       fill=color, anchor=anchor,
                                       font=(family, -self.px(size), weight))

    def _line(self, x1, y1, x2, y2, color=LINE, width=1):
        self.canvas.create_line(self.px(x1), self.px(y1), self.px(x2), self.px(y2),
                                fill=color, width=self.px(width))

    def _rect(self, x1, y1, x2, y2, fill, outline=""):
        self.canvas.create_rectangle(self.px(x1), self.px(y1), self.px(x2), self.px(y2),
                                     fill=fill, outline=outline)

    def _button(self, name, bounds, label, color=MUTED, size=13):
        self.buttons[name] = bounds
        x1, y1, x2, y2 = bounds
        self._text((x1 + x2) / 2, (y1 + y2) / 2, label, size=size, color=color, anchor="center")

    def _resize(self, initial=False):
        width = self.px(self.WIDTH)
        height = self.px(self.COMPACT_HEIGHT if self.settings.compact else self.HEIGHT)
        areas = monitor_workareas(self.root)
        if initial:
            primary = next((r for r in areas if r[0] <= 0 < r[2] and r[1] <= 0 < r[3]), areas[0])
            x = self.settings.x if self.settings.x is not None else primary[2] - width - self.px(28)
            y = self.settings.y if self.settings.y is not None else primary[1] + self.px(72)
        else:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        x, y = fit_position(x, y, width, height, areas)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _gold_old(self, now: datetime) -> bool:
        if not self.snapshot or not self.snapshot.gold:
            return True
        return self.snapshot.gold.is_stale(now)

    def _fx_old(self, now: datetime) -> bool:
        return bool(self.snapshot and self.snapshot.fx and self.snapshot.fx.is_stale(now))

    def _status(self, now: datetime) -> tuple[str, str]:
        if not self.snapshot or not self.snapshot.gold:
            return ("连接中", MUTED) if self.busy or not self.last_error else ("连接失败", RED)
        if self.worker_error or (self.last_error and any("gold" in error.lower() or "金价" in error for error in self.snapshot.errors)):
            return "连接异常", RED
        if self._gold_old(now):
            return "报价延迟", GOLD
        if self.busy and self.settings.interval >= 5:
            return "刷新中", MUTED
        return ("实时", GREEN) if self.settings.source == "swissquote" else ("已连接", GREEN)

    def _draw(self):
        if self.closed:
            return
        now = datetime.now(timezone.utc)
        self._update_ticker(now)
        if self.docked:
            return
        self.canvas.delete("all")
        self.buttons.clear()
        status, status_color = self._status(now)
        self._rect(15, 13, 42, 40, "#3b3325")
        self._text(28.5, 26.5, "Au", size=15, color=GOLD, weight="bold", anchor="center", family="Segoe UI")
        self._text(52, 26, "黄金现货", size=13, weight="bold")
        self._text(135, 26, "●", size=8, color=status_color)
        self._text(148, 26, status, size=10, color=status_color)
        self._button("pin", (201, 9, 248, 44), "已置顶" if self.settings.topmost else "置顶", GOLD if self.settings.topmost else MUTED, 10)
        self._button("compact", (249, 9, 285, 44), "▣" if self.settings.compact else "▤", size=15)
        self._button("minimize", (286, 9, 325, 44), "−", size=19)
        self._button("close", (328, 9, 365, 44), "×", size=20)
        if self.settings.compact:
            self._draw_compact(now)
            return
        self._text(19, 61, "XAU / USD", size=10, color=MUTED, family="Segoe UI", weight="bold")
        self._text(102, 61, "美元 / 金衡盎司", size=10, color=MUTED)
        value = self.snapshot.usd_per_oz if self.snapshot else None
        self._text(17, 96, f"${value:,.2f}" if value is not None else "$ — —", size=39,
                   color=self._price_color() if value is not None else MUTED, weight="bold", family="Segoe UI")
        self._button("refresh", (321, 76, 357, 115), "↻", GOLD if self.busy else MUTED, 24)
        self._text(20, 129, self._session_change(), size=10,
                   color=(GREEN if value >= self.session_open else RED) if value and self.session_open else MUTED)
        self._rect(16, 148, 358, 184, PANEL)
        self._text(26, 166, "人民币 / 克", size=11, color=MUTED)
        cny = self.snapshot.cny_per_gram if self.snapshot else None
        fx_old = self._fx_old(now)
        self._text(346, 166, f"≈ ¥{cny:,.2f}" if cny is not None else "汇率连接中…" if self.busy else "汇率暂无数据",
                   size=16 if cny is not None else 11, color=GOLD if fx_old else WHITE,
                   anchor="e", weight="bold", family="Segoe UI" if cny is not None else "Microsoft YaHei UI")
        self._draw_chart()
        source_name = getattr(self.snapshot.gold, "source_name", "Gold API") if self.snapshot and self.snapshot.gold else ("Swissquote" if self.settings.source == "swissquote" else "Gold API")
        source_note = "Swissquote 买卖中间价" if "Swissquote" in source_name else "Gold API 黄金现货"
        self._text(20, 225, "汇率已过期 · 换算仅供参考" if fx_old else source_note + " · 人民币为换算值", size=9, color=GOLD if fx_old else MUTED)
        self._line(16, 239, 358, 239)
        if time.monotonic() < self.notice_until:
            footer = self.notice
        elif not self.snapshot or not self.snapshot.gold:
            footer = "正在获取最新报价…" if self.busy else "网络异常 · 将自动重试"
        else:
            quote = self.snapshot.gold
            age = (now - quote.quoted_at).total_seconds()
            footer = f"报价 {quote.quoted_at.astimezone():%H:%M:%S} · {age_label(age)}"
            if self._gold_old(now):
                footer = f"旧报价 · {age_label(age)} · 等待更新"
            elif self.last_error:
                footer = "连接异常 · 保留上次报价" if status == "连接异常" else "汇率异常 · 详见数据来源"
        self._text(19, 253, footer, size=9, color=GOLD if self._gold_old(now) and self.snapshot else MUTED)
        self._text(319, 253, f"{self.settings.interval}s", size=9, color=MUTED, anchor="e", family="Segoe UI")
        self._button("menu", (329, 240, 363, 267), "···", size=19)

    def _draw_compact(self, now):
        value = self.snapshot.usd_per_oz if self.snapshot else None
        self._text(17, 68, f"${value:,.2f}" if value is not None else "$ — —", size=31,
                   color=self._price_color() if value else MUTED, weight="bold", family="Segoe UI")
        self._text(20, 105, "USD / oz", size=10, color=MUTED, family="Segoe UI")
        cny = self.snapshot.cny_per_gram if self.snapshot else None
        self._text(346, 69, f"≈ ¥{cny:,.2f}" if cny else "—", size=15, anchor="e", family="Segoe UI")
        self._text(346, 105, "CNY / g · 参考换算" if not self._fx_old(now) else "CNY / g · 汇率过期", size=10,
                   color=GOLD if self._fx_old(now) else MUTED, anchor="e")
        if self.snapshot and self.snapshot.gold:
            label = "旧报价" if self._gold_old(now) else f"{self.snapshot.gold.quoted_at.astimezone():%H:%M:%S}"
            self._text(167, 105, label, size=10, color=MUTED, anchor="center")

    def _session_change(self):
        if not self.snapshot or self.snapshot.usd_per_oz is None or self.session_open is None:
            return "等待第一笔报价"
        change = self.snapshot.usd_per_oz - self.session_open
        percent = change / self.session_open * 100
        return f"本次打开  {change:+,.2f}  ({percent:+.2f}%)"

    def _draw_chart(self):
        if len(self.history) < 2:
            self._line(20, 205, 354, 205)
            self._rect(111, 197, 263, 213, BG)
            self._text(187, 204, "走势将在收到新报价后显示", size=9, color=MUTED, anchor="center")
            return
        values = [float(p) for _, p in self.history]
        low, high = min(values), max(values)
        spread = high - low
        points = []
        first_time = self.history[0][0]
        duration = max(1, (self.history[-1][0] - first_time).total_seconds())
        for timestamp, value in self.history:
            x = 20 + 334 * (timestamp - first_time).total_seconds() / duration
            y = 204 if spread == 0 else 214 - 20 * (float(value) - low) / spread
            points.extend((self.px(x), self.px(y)))
        self.canvas.create_line(*points, fill=GOLD, width=self.px(1.5), capstyle="round", joinstyle="round")
        self.canvas.create_oval(points[-2] - 2, points[-1] - 2, points[-2] + 2, points[-1] + 2, fill=GOLD, outline="")

    def refresh(self, manual=False):
        if self.closed or self.busy:
            return
        now = time.monotonic()
        if now - self.last_request < self.minimum_interval:
            if manual:
                self.notice = f"已是最新请求 · {int(self.minimum_interval - (now - self.last_request)) + 1} 秒后可刷新"
                self.notice_until = now + 3
                self._draw()
            return
        self.busy = True
        self.last_request = now
        self.next_refresh = now + max(self.minimum_interval, self.settings.interval)
        self._draw()

        def fetch():
            try:
                self.results.put((self.client.refresh(), None))
            except Exception as exc:
                logging.exception("Unexpected quote refresh failure")
                self.results.put((None, str(exc)))

        threading.Thread(target=fetch, name="gold-price-fetch", daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        try:
            snapshot, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.worker_error = snapshot is None
            if snapshot is not None:
                previous = self.snapshot.gold if self.snapshot else None
                current = snapshot.gold
                if previous and current and current.quoted_at > previous.quoted_at and not current.is_stale():
                    if getattr(previous, "source_name", "") == getattr(current, "source_name", "") and current.usd_per_oz != previous.usd_per_oz:
                        self.price_direction = 1 if current.usd_per_oz > previous.usd_per_oz else -1
                        self.price_flash_until = time.monotonic() + 0.8
                        if self.flash_job:
                            self.root.after_cancel(self.flash_job)
                        self.flash_job = self.root.after(820, self._draw)
                self.snapshot = snapshot
                self.last_error = "\n".join(snapshot.errors)
                if snapshot.gold and not snapshot.gold.is_stale(datetime.now(timezone.utc)):
                    price = snapshot.usd_per_oz
                    if self.session_open is None:
                        self.session_open = price
                    if not self.history or snapshot.gold.quoted_at > self.history[-1][0]:
                        self.history.append((snapshot.gold.quoted_at, price))
            else:
                self.last_error = error or "暂时无法获取报价"
            if self.pending_source:
                selected = self.pending_source
                self.pending_source = None
                self.set_source(selected)
            self._draw()
        self.poll_job = self.root.after(120, self._poll)

    def _tick(self):
        if self.closed:
            return
        if time.monotonic() >= self.next_refresh:
            self.refresh()
        if self.restore_event and sys.platform == "win32":
            kernel = ctypes.windll.kernel32
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            if kernel.WaitForSingleObject(self.restore_event, 0) == 0:
                self.restore_window()
        self._draw()
        self.tick_job = self.root.after(1000, self._tick)

    def _hit(self, x, y):
        for name, (x1, y1, x2, y2) in self.buttons.items():
            if x1 <= x / self.scale <= x2 and y1 <= y / self.scale <= y2:
                return name
        return None

    def _press(self, event):
        self.root.focus_force()
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y(), self._hit(event.x, event.y))

    def _drag(self, event):
        if not self.drag_origin or self.drag_origin[4]:
            return
        x0, y0, wx, wy, _ = self.drag_origin
        self.root.geometry(f"+{wx + event.x_root - x0}+{wy + event.y_root - y0}")

    def _release(self, event):
        if not self.drag_origin:
            return
        x0, y0, _, _, button = self.drag_origin
        self.drag_origin = None
        if abs(event.x_root - x0) + abs(event.y_root - y0) < self.px(5) and button == self._hit(event.x, event.y):
            actions = {"pin": self.toggle_pin, "compact": self.toggle_compact, "close": self.close,
                       "minimize": self.minimize_to_taskbar,
                       "refresh": lambda: self.refresh(manual=True), "menu": lambda: self.show_menu(event)}
            if button in actions:
                actions[button]()
        if not self.closed and not self.docked:
            self._resize()
            self._save()

    def _double_click(self, event):
        if event.y < self.px(46) and not self._hit(event.x, event.y):
            self.drag_origin = None
            self.toggle_compact()

    def _hover(self, event):
        self.canvas.configure(cursor="hand2" if self._hit(event.x, event.y) else "fleur" if event.y < self.px(46) else "arrow")

    def _save(self):
        if not self.docked and self.root.state() != "iconic":
            self.settings.x = self.root.winfo_x()
            self.settings.y = self.root.winfo_y()
        self.settings.save(self.settings_path)

    def _on_unmap(self, event):
        if event.widget is self.root and not self.closed and not self.docked and self.root.state() == "iconic":
            if self.dock_job is None:
                self.dock_job = self.root.after_idle(self._dock_after_minimize)

    def _dock_after_minimize(self):
        self.dock_job = None
        self.minimize_to_taskbar()

    def _save_taskbar_position(self, x, y):
        self.settings.taskbar_x, self.settings.taskbar_y = x, y
        self.settings.save(self.settings_path)

    def minimize_to_taskbar(self):
        if self.closed or self.docked:
            return
        from .taskbar import TaskbarTicker
        if self.ticker is None:
            self.ticker = TaskbarTicker(self.root, on_restore=self.restore_window, on_menu=self.show_menu,
                                        on_position=self._save_taskbar_position,
                                        x=self.settings.taskbar_x, y=self.settings.taskbar_y, scale=self.scale)
        self._save()
        self.docked = True
        self.settings.docked = True
        self.root.withdraw()
        try:
            self.ticker.show()
        except Exception:
            logging.exception("Could not show taskbar price strip")
            self.restore_window()
            return
        self._update_ticker(datetime.now(timezone.utc))
        self.settings.save(self.settings_path)

    def restore_window(self):
        if self.closed:
            return
        self._close_context_menu()
        if self.ticker:
            self.ticker.hide()
        self.docked = False
        self.settings.docked = False
        self.root.deiconify()
        self._resize()
        self.root.lift()
        self.root.focus_force()
        self._save()
        self._draw()

    def set_source(self, source):
        if source == self.settings.source and self.pending_source is None:
            return
        if self.busy:
            self.pending_source = source
            return
        self.settings.source = source
        self.settings.interval = 1 if source == "swissquote" else 30
        self.client = PriceClient(timeout=5, source=source)
        self.snapshot = None
        self.session_open = None
        self.history.clear()
        self.last_error = ""
        self.worker_error = False
        self.price_direction = 0
        self.price_flash_until = 0
        self.last_request = float("-inf")
        self._save()
        self.refresh()

    def toggle_pin(self):
        self.settings.topmost = not self.settings.topmost
        self.root.attributes("-topmost", self.settings.topmost)
        self._save()
        self._draw()

    def toggle_compact(self):
        self.settings.compact = not self.settings.compact
        self._resize()
        self._save()
        self._draw()

    def set_interval(self, interval):
        self.settings.interval = max(self.minimum_interval, interval)
        self.next_refresh = max(time.monotonic(), self.last_request + self.settings.interval)
        self._save()
        self._draw()

    def set_opacity(self, opacity):
        self.settings.opacity = opacity
        self.root.attributes("-alpha", opacity)
        self._save()

    def show_menu(self, event):
        if self.closed:
            return
        if self.docked and self.ticker:
            self._show_taskbar_menu()
            return
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL, fg=WHITE, activebackground=LINE,
                       activeforeground=GOLD, bd=0, font=("Microsoft YaHei UI", 10))
        menu.add_command(label="立即刷新  F5", command=lambda: self.refresh(manual=True))
        menu.add_command(label="恢复悬浮窗" if self.docked else "收起到任务栏  Ctrl+H",
                         command=self.restore_window if self.docked else self.minimize_to_taskbar)
        menu.add_command(label="取消置顶  Ctrl+P" if self.settings.topmost else "置顶显示  Ctrl+P", command=self.toggle_pin)
        menu.add_command(label="展开窗口  Ctrl+M" if self.settings.compact else "精简模式  Ctrl+M", command=self.toggle_compact)
        interval_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=WHITE)
        self.interval_var = tk.IntVar(value=self.settings.interval)
        for interval in ((1, 2, 5, 30) if self.settings.source == "swissquote" else (30, 60, 120)):
            interval_menu.add_radiobutton(label=f"每 {interval} 秒", value=interval, variable=self.interval_var,
                                          command=lambda i=interval: self.set_interval(i))
        menu.add_cascade(label="刷新频率", menu=interval_menu)
        source_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=WHITE)
        self.source_var = tk.StringVar(value=self.pending_source or self.settings.source)
        source_menu.add_radiobutton(label="Swissquote · 秒级现货中间价", value="swissquote", variable=self.source_var,
                                    command=lambda: self.set_source("swissquote"))
        source_menu.add_radiobutton(label="Gold API · 30 秒现货参考价", value="goldapi", variable=self.source_var,
                                    command=lambda: self.set_source("goldapi"))
        menu.add_cascade(label="行情来源", menu=source_menu)
        alpha_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=WHITE)
        self.alpha_var = tk.DoubleVar(value=self.settings.opacity)
        for alpha in (1.0, 0.9, 0.8):
            alpha_menu.add_radiobutton(label=f"{int(alpha * 100)}%", value=alpha, variable=self.alpha_var,
                                       command=lambda a=alpha: self.set_opacity(a))
        menu.add_cascade(label="不透明度", menu=alpha_menu)
        menu.add_separator()
        menu.add_command(label="数据来源与状态", command=self.show_about)
        menu.add_separator()
        menu.add_command(label="退出", command=self.close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
                menu.destroy()
            except tk.TclError:
                pass  # An Exit command may already have destroyed the root.

    def _close_context_menu(self):
        if self.context_menu is not None:
            self.context_menu.close()

    def _show_taskbar_menu(self):
        from .popup import MenuItem, PopupMenu

        self._close_context_menu()
        ticker = self.ticker
        anchor, work_area = ticker.begin_menu()
        intervals = (1, 2, 5, 30) if self.settings.source == "swissquote" else (30, 60, 120)
        selected_source = self.pending_source or self.settings.source
        items = [
            MenuItem("恢复悬浮窗", command=self.restore_window),
            MenuItem("立即刷新", command=lambda: self.refresh(manual=True), accelerator="F5"),
            MenuItem("刷新频率", children=tuple(
                MenuItem(f"每 {interval} 秒", command=lambda i=interval: self.set_interval(i),
                         checked=self.settings.interval == interval) for interval in intervals)),
            MenuItem("行情来源", children=(
                MenuItem("Swissquote · 秒级现货", command=lambda: self.set_source("swissquote"),
                         checked=selected_source == "swissquote"),
                MenuItem("Gold API · 30 秒参考价", command=lambda: self.set_source("goldapi"),
                         checked=selected_source == "goldapi"))),
            MenuItem("数据来源与状态", command=self.show_about),
            MenuItem("退出", command=self.close),
        ]

        def dismissed():
            self.context_menu = None
            ticker.end_menu()

        try:
            self.context_menu = PopupMenu(self.root, items, anchor=anchor, work_area=work_area,
                                          scale=self.scale, light=ticker.light_theme,
                                          on_close=dismissed)
            self.context_menu.show()
        except Exception:
            self._close_context_menu()
            dismissed()
            logging.exception("Could not open taskbar menu")

    def show_about(self):
        if self.about_window and self.about_window.winfo_exists():
            self.about_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.about_window = window
        window.title("数据来源 · Gold Monitor")
        window.configure(bg=BG, padx=22, pady=18)
        window.attributes("-topmost", self.settings.topmost)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        def label(text, color=WHITE, size=10):
            item = tk.Label(window, text=text, bg=BG, fg=color, justify="left", wraplength=self.px(400),
                            font=("Microsoft YaHei UI", size))
            item.pack(anchor="w", pady=4)
            return item

        label("Gold Monitor  /  黄金现货", GOLD, 15)
        label("秒级报价：Swissquote · XAU/USD 现货买卖中间价\n默认每秒查询；收到真实价格变化时闪烁显示涨跌。")
        self._link(window, "Swissquote 官方产品指南  ↗", "https://library.swissquote.com/cdn/2958/ql/forex-product-guide-ltd-en")
        label("备选：Gold API 现货参考价，每 30 秒起查询。\n不同来源存在价差，切换来源会重置本次走势。", MUTED, 9)
        self._link(window, "Gold API  ↗", "https://gold-api.com/")
        label("汇率：ExchangeRate-API · USD/CNY 参考汇率，通常每日更新。\n人民币 / 克 = 美元 / 盎司 × USD/CNY ÷ 31.1034768。")
        self._link(window, "Rates by ExchangeRate-API  ↗", "https://www.exchangerate-api.com/")
        label("行情可能存在延迟；休市时保留最后报价并标记延迟。\n换算值不包含零售溢价、加工费或买卖价差。", MUTED, 9)
        if self.snapshot:
            if self.snapshot.gold:
                label(f"金价时间：{self.snapshot.gold.quoted_at.astimezone():%Y-%m-%d %H:%M:%S %Z}", MUTED, 9)
            if self.snapshot.fx:
                label(f"汇率时间：{self.snapshot.fx.updated_at.astimezone():%Y-%m-%d %H:%M:%S %Z}", MUTED, 9)
        if self.last_error:
            label(f"最近连接状态：\n{self.last_error[:600]}", GOLD, 9)
        label("拖动窗口移动 · 双击标题折叠\n− 或 Ctrl+H 收起为任务栏金价条，点击金价条恢复。\nF5 刷新 · Ctrl+P 置顶 · Ctrl+M 精简 · Alt+F4 退出", MUTED, 9)
        tk.Button(window, text="知道了", command=window.destroy, bg=PANEL, fg=WHITE,
                  relief="flat", padx=18, pady=6).pack(anchor="e", pady=(12, 0))
        window.update_idletasks()
        x, y = fit_position(self.root.winfo_x() - 30, self.root.winfo_y() + 30,
                            window.winfo_reqwidth(), window.winfo_reqheight(), monitor_workareas(self.root))
        window.geometry(f"+{x}+{y}")

    def _link(self, parent, text, url):
        link = tk.Label(parent, text=text, bg=BG, fg=GOLD, cursor="hand2", font=("Segoe UI", 10, "underline"))
        link.pack(anchor="w", pady=3)
        link.bind("<Button-1>", lambda _e: webbrowser.open(url))

    def close(self):
        if self.closed:
            return
        self._close_context_menu()
        self._save()
        self.closed = True
        if self.ticker:
            self.ticker.destroy()
        for job in (self.poll_job, self.tick_job, self.refresh_job, self.flash_job, self.dock_job):
            if job is None:
                continue
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self.root.destroy()


def _single_instance():
    if sys.platform != "win32":
        return None, True
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\GoldMonitor.DesktopWidget.v1")
    if kernel.GetLastError() == 183:
        kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        event = kernel.OpenEventW(0x0002, False, "Local\\GoldMonitor.Restore.v1")
        if event:
            kernel.SetEvent(event)
            kernel.CloseHandle(event)
        user = ctypes.windll.user32
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        hwnd = user.FindWindowW(None, TITLE)
        if hwnd:
            user.SetForegroundWindow.argtypes = [wintypes.HWND]
            user.SetForegroundWindow(hwnd)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        return None, False
    return handle, True


def _create_restore_event():
    if sys.platform != "win32":
        return None
    kernel = ctypes.windll.kernel32
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    return kernel.CreateEventW(None, False, False, "Local\\GoldMonitor.Restore.v1")


def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    mutex, should_run = _single_instance()
    if not should_run:
        return
    directory = data_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=directory / "gold-monitor.log", level=logging.WARNING,
                            format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    except OSError:
        logging.basicConfig(level=logging.WARNING)
    root = tk.Tk()
    root.report_callback_exception = lambda kind, value, tb: logging.error("UI callback failed", exc_info=(kind, value, tb))
    root.withdraw()
    app = GoldMonitor(root, directory)
    app.restore_event = _create_restore_event()
    show_in_taskbar(root)
    root.deiconify()
    root.update_idletasks()
    if app.settings.docked:
        app.minimize_to_taskbar()
    try:
        root.mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.windll.kernel32.CloseHandle(mutex)
        if app.restore_event:
            ctypes.windll.kernel32.CloseHandle(app.restore_event)


if __name__ == "__main__":
    main()
