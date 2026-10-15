"""A non-activating quote strip that uses free space in the Windows taskbar.

The strip is our own window; Explorer is never modified or reparented. Read-only
UI Automation checks keep it away from taskbar buttons. If that check is not
available, or the taskbar is full, it sits immediately beside the taskbar.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable
import uuid


Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class TaskbarArea:
    rect: Rect
    monitor: Rect
    work: Rect
    occupied: tuple[Rect, ...] = ()
    verified: bool = False
    hidden: bool = False
    fullscreen: bool = False
    handle: int = 0


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    width: int
    height: int
    in_taskbar: bool
    reason: str


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def place_strip(area: TaskbarArea, width: int, height: int,
                preferred_x: int | None = None, margin: int = 6) -> Placement:
    """Find a verified horizontal gap, otherwise use the adjacent work area."""
    left, top, right, bottom = area.rect
    horizontal = right - left > bottom - top
    if horizontal and area.verified and bottom - top >= height:
        intervals = sorted((max(left, r[0] - margin), min(right, r[2] + margin))
                           for r in area.occupied if _overlaps(area.rect, r))
        gaps = []
        cursor = left + margin
        for start, end in intervals:
            if start - cursor >= width:
                gaps.append((cursor, start - width))
            cursor = max(cursor, end)
        if right - margin - cursor >= width:
            gaps.append((cursor, right - margin - width))
        if gaps:
            desired = preferred_x if preferred_x is not None else right - width - margin
            x = min((max(start, min(desired, end)) for start, end in gaps),
                    key=lambda value: abs(value - desired))
            return Placement(x, top + (bottom - top - height) // 2,
                             width, height, True, "taskbar-gap")

    work_left, work_top, work_right, work_bottom = area.work
    width = min(width, max(1, work_right - work_left - 2 * margin))
    height = min(height, max(1, work_bottom - work_top - 2 * margin))
    x = work_right - width - margin if preferred_x is None else preferred_x
    x = max(work_left + margin, min(x, work_right - width - margin))
    if horizontal:
        y = work_top + margin if top <= area.monitor[1] else work_bottom - height - margin
    else:
        x = work_left + margin if left <= area.monitor[0] else work_right - width - margin
        y = work_bottom - height - margin
    reason = "taskbar-full" if area.verified else "layout-unavailable"
    if not horizontal:
        reason = "vertical-taskbar"
    return Placement(x, y, width, height, False, reason)


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def make(cls, text: str):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


def _com_call(pointer, index, result_type, arg_types, *args):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arg_types)(table[index])
    return function(pointer, *args)


def _release(pointer):
    if pointer:
        _com_call(pointer, 2, ctypes.c_ulong, [])


class _Accessibility:
    """Small read-only UIA client. Called only by the locator's background thread.

    Vtable slots follow Microsoft's UIAutomationClient.h. No element names,
    document contents, input events, or application UI are read.
    """

    def __init__(self):
        self.ole = ctypes.OleDLL("ole32")
        self.ole.CoInitializeEx(None, 0)  # A dedicated multithreaded apartment.
        self.pointer = ctypes.c_void_p()
        self.condition = ctypes.c_void_p()
        clsid = _GUID.make("FF48DBA4-60EF-4201-AA87-54103EEF594E")
        iid = _GUID.make("30CBE57D-D9D0-452A-AB13-7AC5AC4825EE")
        self.ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                                  ctypes.byref(self.pointer))
        self._call(self.pointer, 21, [], ctypes.byref(self.condition))

    @staticmethod
    def _call(pointer, index, types, *args):
        # All methods here have exactly one output parameter, a pointer.
        result = _com_call(pointer, index, ctypes.c_long,
                           [*types, ctypes.c_void_p], *args)
        if result < 0:
            raise OSError(f"UI Automation failed: {result & 0xffffffff:08x}")

    def rectangles(self, handle: int, taskbar: Rect) -> tuple[tuple[Rect, ...], bool]:
        element = ctypes.c_void_p()
        elements = ctypes.c_void_p()
        found = []
        controls = 0
        try:
            self._call(self.pointer, 6, [wintypes.HWND], handle, ctypes.byref(element))
            self._call(element, 6, [ctypes.c_int, ctypes.c_void_p], 4,
                       self.condition, ctypes.byref(elements))  # TreeScope_Descendants
            count = ctypes.c_int()
            self._call(elements, 3, [], ctypes.byref(count))
            if count.value > 350:  # Unexpected tree; never claim the layout is safe.
                return (), False
            for index in range(count.value):
                child = ctypes.c_void_p()
                try:
                    self._call(elements, 4, [ctypes.c_int], index, ctypes.byref(child))
                    kind, offscreen, rect = ctypes.c_int(), wintypes.BOOL(), wintypes.RECT()
                    self._call(child, 21, [], ctypes.byref(kind))
                    self._call(child, 38, [], ctypes.byref(offscreen))
                    self._call(child, 43, [], ctypes.byref(rect))
                    bounds = (rect.left, rect.top, rect.right, rect.bottom)
                    # Layout containers span blank space; actual controls reserve it.
                    containers = {50008, 50009, 50018, 50021, 50023, 50028,
                                  50030, 50032, 50033, 50026, 50036, 50040}
                    if (not offscreen.value and kind.value not in containers
                            and _overlaps(bounds, taskbar)
                            and 0 < rect.right - rect.left < (taskbar[2] - taskbar[0]) * 0.85):
                        found.append(bounds)
                        if kind.value in {50000, 50002, 50003, 50007, 50011, 50031}:
                            controls += 1
                finally:
                    _release(child)
            return tuple(found), controls >= 2
        finally:
            _release(elements)
            _release(element)

    def close(self):
        _release(self.condition)
        _release(self.pointer)
        self.ole.CoUninitialize()


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _tuple(rect) -> Rect:
    return rect.left, rect.top, rect.right, rect.bottom


def _primary_workarea() -> Rect | None:
    """SPI_GETWORKAREA excludes the primary taskbar before UIA is ready."""
    if sys.platform != "win32":
        return None
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                           ctypes.c_void_p, wintypes.UINT]
    rect = wintypes.RECT()
    if user.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        if rect.right > rect.left and rect.bottom > rect.top:
            return _tuple(rect)
    return None


def initial_taskbar_area(width: int, height: int, work: Rect | None) -> TaskbarArea:
    """Use only a known work area; never guess that screen-bottom is free."""
    monitor = (0, 0, width, height)
    if work is not None:
        left, top, right, bottom = work
        # The largest reserved edge normally belongs to the taskbar. Whichever
        # edge is chosen, fallback placement remains entirely in the work area.
        reserved = [(left, (0, 0, left, height)),
                    (top, (0, 0, width, top)),
                    (width - right, (right, 0, width, height)),
                    (height - bottom, (0, bottom, width, height))]
        thickness, rect = max(reserved, key=lambda entry: entry[0])
        if thickness > 0 and 0 <= left < right <= width and 0 <= top < bottom <= height:
            return TaskbarArea(rect, monitor, work)
    # This also covers auto-hide (work area == monitor). Wait for actual shell
    # visibility rather than briefly covering its buttons while it animates.
    return TaskbarArea((0, height, width, height), monitor, work or monitor, hidden=True)


def _suppress_for_desktop(area: TaskbarArea) -> bool:
    """Fast visibility checks, independent of a potentially slow UIA scan."""
    if sys.platform != "win32":
        return False
    user = ctypes.WinDLL("user32")
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    rect = wintypes.RECT()
    screen = area.monitor
    if area.handle and user.GetWindowRect(area.handle, ctypes.byref(rect)):
        visible_width = min(rect.right, screen[2]) - max(rect.left, screen[0])
        visible_height = min(rect.bottom, screen[3]) - max(rect.top, screen[1])
        if min(visible_width, visible_height) <= 3:
            return True
    foreground = user.GetForegroundWindow()
    if not foreground:
        return False
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    name = ctypes.create_unicode_buffer(256)
    user.GetClassNameW(foreground, name, len(name))
    if name.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
        return False
    return bool(user.GetWindowRect(foreground, ctypes.byref(rect))
                and rect.left <= screen[0] + 2 and rect.top <= screen[1] + 2
                and rect.right >= screen[2] - 2 and rect.bottom >= screen[3] - 2)


class WindowsTaskbars:
    """Read the shell layout without sending input or changing the shell."""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        api = self.user
        api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        api.IsWindowVisible.argtypes = [wintypes.HWND]
        api.GetForegroundWindow.restype = wintypes.HWND
        api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        api.MonitorFromWindow.restype = wintypes.HANDLE
        api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
        self.accessibility = None
        try:
            self.accessibility = _Accessibility()
        except (OSError, AttributeError):
            logging.debug("Taskbar accessibility unavailable; using adjacent position", exc_info=True)

    def _class_name(self, handle):
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(handle, name, len(name))
        return name.value

    def scan(self) -> list[TaskbarArea]:
        areas = []
        api = self.user
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        foreground = api.GetForegroundWindow()
        foreground_rect = wintypes.RECT()
        api.GetWindowRect(foreground, ctypes.byref(foreground_rect))
        foreground_pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
        kernel = ctypes.WinDLL("kernel32")
        foreground_is_app = (foreground_pid.value != kernel.GetCurrentProcessId()
                             and self._class_name(foreground)
                             not in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})

        @callback_type
        def collect(handle, _data):
            try:
                if self._class_name(handle) not in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
                    return True
                rect = wintypes.RECT()
                if not api.GetWindowRect(handle, ctypes.byref(rect)):
                    return True
                info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
                monitor = api.MonitorFromWindow(handle, 2)
                if not api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    return True
                bounds, screen = _tuple(rect), _tuple(info.rcMonitor)
                visible_width = min(bounds[2], screen[2]) - max(bounds[0], screen[0])
                visible_height = min(bounds[3], screen[3]) - max(bounds[1], screen[1])
                hidden = not api.IsWindowVisible(handle) or min(visible_width, visible_height) <= 3
                full = (foreground_is_app and foreground_rect.left <= screen[0] + 2
                        and foreground_rect.top <= screen[1] + 2
                        and foreground_rect.right >= screen[2] - 2
                        and foreground_rect.bottom >= screen[3] - 2)
                occupied, verified = (), False
                if self.accessibility and not hidden and not full:
                    try:
                        occupied, verified = self.accessibility.rectangles(handle, bounds)
                    except OSError:
                        pass
                # The notification area is reserved even if UIA misses a button.
                child_bounds = []

                @callback_type
                def protect(child, _):
                    if (self._class_name(child) in {"TrayNotifyWnd", "TrayClockWClass", "ClockButton",
                                                   "TrayShowDesktopButton", "Start", "TrayButton"}
                            and api.IsWindowVisible(child)):
                        child_rect = wintypes.RECT()
                        if api.GetWindowRect(child, ctypes.byref(child_rect)):
                            child_bounds.append(_tuple(child_rect))
                    return True

                api.EnumChildWindows(handle, protect, 0)
                areas.append(TaskbarArea(bounds, screen, _tuple(info.rcWork),
                                         (*occupied, *child_bounds), verified, hidden, full, int(handle)))
            except Exception:
                logging.debug("Taskbar layout changed while being read", exc_info=True)
            return True

        api.EnumWindows(collect, 0)
        return areas

    def close(self):
        if self.accessibility:
            self.accessibility.close()


class TaskbarTicker:
    HEIGHT = 32

    def __init__(self, root: tk.Tk, on_restore: Callable[[], None],
                 on_menu: Callable | None = None, on_position: Callable | None = None,
                 *, x: int | None = None, y: int | None = None, scale: float = 1.0):
        self.root = root
        self.on_restore = on_restore
        self.on_menu = on_menu
        self.on_position = on_position
        self.scale = max(1.0, scale)
        self.preferred_x, self.preferred_y = x, y
        self.visible = False  # Desired visibility; an auto-hidden taskbar may suppress mapping.
        self.closed = False
        self.in_taskbar = False
        self.placement_reason = "locating-taskbar"
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title("Gold Monitor · 任务栏金价")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        if sys.platform == "win32":
            self.window.attributes("-toolwindow", True)
        self.light_theme = self._read_light_theme()
        self._theme_checked = time.monotonic()
        self.canvas = tk.Canvas(self.window, bg=self._fallback_background(), highlightthickness=0,
                                bd=0, cursor="hand2")
        self.window.configure(bg=self._fallback_background())
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        if on_menu:
            self.canvas.bind("<Button-3>", on_menu)
        self.window.bind("<Escape>", lambda _event: self.on_restore())
        self.primary, self.secondary = "等待金价…", "点击打开 · 右键菜单"
        self.color, self.status = "#f2cb79", ""
        self.primary_font = tkfont.Font(root=root, family="Segoe UI", size=-round(13 * self.scale), weight="bold")
        self.secondary_font = tkfont.Font(root=root, family="Microsoft YaHei UI", size=-round(10 * self.scale))
        self.surface = None
        self._surface_failed = False
        self._render_lines = []
        self._render_signature = None
        self.content_width = round(120 * self.scale)
        self.content_height = round(self.HEIGHT * self.scale)
        self._last_geometry = None
        self._drag_origin = None
        self._dragged = False
        self.menu_open = False
        self._areas: list[TaskbarArea] = []
        self._results: queue.Queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._active = threading.Event()
        self._worker = None
        self._job = None
        self.hwnd = 0
        self._draw()

    def _start_locator(self):
        if self._worker is not None or sys.platform != "win32":
            return
        self._worker = threading.Thread(target=self._locate, name="taskbar-layout", daemon=True)
        self._worker.start()

    def _locate(self):
        locator = None
        try:
            locator = WindowsTaskbars()
            while not self._stop.is_set():
                if not self._active.wait(0.5):
                    continue
                if self._stop.is_set():
                    break
                areas = locator.scan()
                try:
                    self._results.get_nowait()
                except queue.Empty:
                    pass
                self._results.put_nowait((time.monotonic(), areas))
                self._stop.wait(1.0)
        except Exception:
            logging.exception("Taskbar layout worker stopped")
        finally:
            if locator:
                locator.close()

    def show(self):
        if self.closed:
            return
        self.visible = True
        self._active.set()
        self._start_locator()
        self._synchronize()

    def hide(self):
        self.visible = False
        self.menu_open = False
        self._active.clear()
        self.window.withdraw()
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None

    def begin_menu(self):
        """Keep the anchor and Z order stable while its popup receives input."""
        self.menu_open = True
        self._drag_origin = None
        area = self._choose_area()
        anchor = self._last_geometry or (self.window.winfo_rootx(), self.window.winfo_rooty(),
                                         self.content_width, self.content_height)
        return anchor, area.work

    def end_menu(self):
        self.menu_open = False

    def update_price(self, primary: str, secondary: str, *, color: str = "#f2cb79", status: str = ""):
        value = (primary, secondary, color, status)
        if value != (self.primary, self.secondary, self.color, self.status):
            self.primary, self.secondary, self.color, self.status = value
            self._draw()

    @staticmethod
    def _read_light_theme():
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
            except OSError:
                pass
        return True

    def _fallback_background(self):
        return "#e8eef5" if self.light_theme else "#202020"

    def _text_colors(self):
        if self.light_theme:
            primary = {"#69d5b0": "#08784f", "#ee8991": "#c03948"}.get(self.color, "#263545")
            return primary, "#536172"
        return self.color if self.color in ("#69d5b0", "#ee8991") else "#edf2f7", "#aeb8c4"

    def _secondary_text(self):
        status = "" if self.status in ("实时", "已连接", "刷新中") else self.status
        return f"{self.secondary}  {status}".rstrip()

    def _draw(self):
        c, s = self.canvas, self.scale
        c.delete("all")
        padding = round(6 * s)
        arrow_width = self.primary_font.measure("↓ ")
        has_arrow = self.primary.startswith(("↑ ", "↓ "))
        price_text = self.primary[2:] if has_arrow else self.primary
        secondary = self._secondary_text()
        # Reserve only the direction glyph and measured text, using stable digit
        # widths so normal ticks do not make the strip hop left and right.
        measured_price = self.primary_font.measure("".join("8" if ch.isdecimal() else ch for ch in price_text))
        measured_secondary = self.secondary_font.measure("".join("8" if ch.isdecimal() else ch for ch in secondary))
        text_width = max(measured_price + arrow_width, measured_secondary + arrow_width)
        self.content_width = text_width + 2 * padding
        self.content_height = round(self.HEIGHT * s)
        primary_color, secondary_color = self._text_colors()
        primary_x = padding if has_arrow else padding + arrow_width
        secondary_x = padding + arrow_width
        primary_y = round(1 * s)
        secondary_y = round(18 * s)
        c.create_text(primary_x, primary_y, anchor="nw", text=self.primary,
                      fill=primary_color, font=self.primary_font)
        c.create_text(secondary_x, secondary_y, anchor="nw", text=secondary,
                      fill=secondary_color, font=self.secondary_font)
        self._render_lines = [
            dict(text=self.primary, family="Segoe UI", size=round(13 * s), bold=True,
                 color=primary_color, x=primary_x, y=primary_y),
            dict(text=secondary, family="Microsoft YaHei UI", size=round(10 * s), bold=False,
                 color=secondary_color, x=secondary_x, y=secondary_y),
        ]
        self._paint_surface()

    def _paint_surface(self, force=False):
        if self.surface is None or not self._last_geometry:
            return
        width, height = self._last_geometry[2:]
        background = None if self.in_taskbar else self._fallback_background()
        signature = (width, height, background, tuple(tuple(line.items()) for line in self._render_lines))
        if not force and signature == self._render_signature:
            return
        try:
            from .taskbar_surface import TextLine
            self.surface.render(width, height, [TextLine(**line) for line in self._render_lines],
                                background=background, force=force)
            self._render_signature = signature
        except Exception:
            logging.exception("Transparent taskbar drawing unavailable; using theme background")
            self.surface.clear()
            self.surface = None
            self._surface_failed = True

    def _choose_area(self):
        if not self._areas:
            width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            return initial_taskbar_area(width, height, _primary_workarea())
        x = self.preferred_x if self.preferred_x is not None else 0
        y = self.preferred_y if self.preferred_y is not None else 0
        def distance(area):
            r = area.monitor
            return max(r[0] - x, 0, x - r[2]) ** 2 + max(r[1] - y, 0, y - r[3]) ** 2
        return min(self._areas, key=distance)

    def _synchronize(self):
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        if not self.visible or self.closed:
            return
        if self.menu_open:
            # Price drawing remains live. Geometry and periodic topmost raises
            # must not race the focusable popup or move its anchor underneath it.
            self._job = self.root.after(250, self._synchronize)
            return
        if time.monotonic() - self._theme_checked >= 2:
            self._theme_checked = time.monotonic()
            light = self._read_light_theme()
            if light != self.light_theme:
                self.light_theme = light
                self.canvas.configure(bg=self._fallback_background())
                self.window.configure(bg=self._fallback_background())
                self._draw()
        try:
            sampled, self._areas = self._results.get_nowait()
            self._sampled = sampled
        except queue.Empty:
            pass
        area = self._choose_area()
        # An unresponsive Explorer cannot leave stale controls covered indefinitely.
        stale = time.monotonic() - getattr(self, "_sampled", 0) > 4
        if stale and area.verified:
            area = TaskbarArea(area.rect, area.monitor, area.work, hidden=area.hidden,
                               fullscreen=area.fullscreen, handle=area.handle)
        if area.hidden or area.fullscreen or _suppress_for_desktop(area):
            self.window.withdraw()
        elif not self._drag_origin:
            placement = place_strip(area, self.content_width, self.content_height,
                                    self.preferred_x, round(6 * self.scale))
            self.in_taskbar = placement.in_taskbar
            self.placement_reason = placement.reason
            self._place(placement)
        self._job = self.root.after(250, self._synchronize)

    def _place(self, placement):
        self.in_taskbar = placement.in_taskbar
        self.placement_reason = placement.reason
        geometry = (placement.x, placement.y, placement.width, placement.height)
        mapped = bool(self.window.winfo_ismapped())
        if geometry != self._last_geometry:
            # + followed by a negative coordinate is supported by Tk; using a
            # bare minus would instead mean an offset from the right/bottom edge.
            self.window.geometry(f"{placement.width}x{placement.height}+{placement.x}+{placement.y}")
            self._last_geometry = geometry
        if sys.platform == "win32" and (not mapped or geometry != getattr(self, "_native_geometry", None)
                                         or time.monotonic() - getattr(self, "_last_raise", 0) >= 1):
            self.window.update_idletasks()
            user = ctypes.WinDLL("user32")
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            self.hwnd = user.GetAncestor(self.window.winfo_id(), 2)
            user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            style = user.GetWindowLongPtrW(self.hwnd, -20)
            user.SetWindowLongPtrW(self.hwnd, -20, (style | 0x08000080) & ~0x40000)
            if self.surface is None and not self._surface_failed:
                try:
                    from .taskbar_surface import NativeTextSurface
                    self.surface = NativeTextSurface(self.hwnd)
                except Exception:
                    logging.exception("Transparent taskbar surface unavailable")
                    self._surface_failed = True
            if not mapped:
                self.window.deiconify()
                self.window.update_idletasks()
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user.SetWindowPos(self.hwnd, -1, placement.x, placement.y,
                              placement.width, placement.height, 0x0010 | 0x0040)
            self._native_geometry = geometry
            self._last_raise = time.monotonic()
            self._render_signature = None
            self._paint_surface(force=not mapped)
        elif not mapped:
            self.window.deiconify()

    def _press(self, event):
        self._drag_origin = (event.x_root, event.y_root,
                             self.window.winfo_x(), self.window.winfo_y())
        self._dragged = False

    def _drag(self, event):
        if self._drag_origin:
            ox, oy, wx, wy = self._drag_origin
            dx, dy = event.x_root - ox, event.y_root - oy
            if abs(dx) + abs(dy) >= 5:
                self._dragged = True
                self.preferred_x, self.preferred_y = wx + dx, wy + dy
                area = self._choose_area()
                placement = place_strip(area, self.content_width, self.content_height,
                                        self.preferred_x, round(6 * self.scale))
                self._place(placement)

    def _release(self, _event):
        if not self._drag_origin:
            return
        self._drag_origin = None
        if self._dragged:
            self.preferred_x, self.preferred_y = self.window.winfo_x(), self.window.winfo_y()
            if self.on_position:
                self.on_position(self.preferred_x, self.preferred_y)
        else:
            self.on_restore()

    def destroy(self):
        if self.closed:
            return
        self.hide()
        self.closed = True
        self._stop.set()
        self._active.set()
        if self.surface:
            self.surface.clear()
            self.surface = None
        self.window.destroy()
