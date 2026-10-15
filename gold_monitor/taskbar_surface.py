"""Per-pixel taskbar text, drawn by Windows without an opaque Tk rectangle.

The almost invisible background has alpha 1/255 instead of zero. Windows
passes clicks through zero-alpha layered pixels, so retaining that one alpha
step keeps the complete quote strip available for clicking and dragging.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
from typing import Sequence


@dataclass(frozen=True)
class TextLine:
    text: str
    x: int
    y: int
    color: str
    family: str = "Segoe UI"
    size: int = 13  # Character height in physical pixels, not points.
    bold: bool = False


class _BitmapHeader(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bit_count", wintypes.WORD), ("compression", wintypes.DWORD),
                ("size_image", wintypes.DWORD), ("x_pixels", wintypes.LONG),
                ("y_pixels", wintypes.LONG), ("used", wintypes.DWORD),
                ("important", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapHeader), ("colors", wintypes.DWORD * 1)]


class _Blend(ctypes.Structure):
    _fields_ = [("operation", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                ("constant_alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]


def _rgb(color: str) -> tuple[int, int, int]:
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("Surface colors must be #rrggbb")
    return tuple(int(color[offset:offset + 2], 16) for offset in (1, 3, 5))


class NativeTextSurface:
    """Supply a premultiplied BGRA surface to an existing Tk wrapper HWND.

    Call on the Tk thread after geometry and mapping are updated. The existing
    canvas remains the input target; its normal paints cannot overwrite this
    per-pixel surface. Do not combine this with Tk's alpha/transparentcolor
    attributes, which use SetLayeredWindowAttributes instead.
    """

    _LAYERED = 0x00080000

    def __init__(self, hwnd: int):
        if sys.platform != "win32":
            raise OSError("Native taskbar surfaces require Windows")
        self.hwnd = hwnd
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self._active = False
        self._last = None
        self._configure_api()

    def _configure_api(self):
        user, gdi = self.user, self.gdi
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC,
                                            ctypes.POINTER(wintypes.POINT),
                                            ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                            ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                            ctypes.POINTER(_Blend), wintypes.DWORD]
        user.UpdateLayeredWindow.restype = wintypes.BOOL
        gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi.CreateCompatibleDC.restype = wintypes.HDC
        gdi.DeleteDC.argtypes = [wintypes.HDC]
        gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo),
                                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
        gdi.CreateDIBSection.restype = wintypes.HBITMAP
        gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi.SelectObject.restype = wintypes.HANDLE
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
        gdi.CreateFontW.restype = wintypes.HFONT
        gdi.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        gdi.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                               wintypes.LPCWSTR, ctypes.c_int]
        gdi.TextOutW.restype = wintypes.BOOL
        gdi.GdiFlush.argtypes = []
        gdi.GdiFlush.restype = wintypes.BOOL

    def _style(self, style: int):
        ctypes.set_last_error(0)
        previous = self.user.SetWindowLongPtrW(self.hwnd, -20, style)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())

    def _enable(self):
        style = self.user.GetWindowLongPtrW(self.hwnd, -20)
        if not self._active or not style & self._LAYERED:
            # Clear a previous SetLayeredWindowAttributes state, if any.
            if style & self._LAYERED:
                self._style(style & ~self._LAYERED)
            self._style(style | self._LAYERED)
            self._active = True
            self._last = None

    def clear(self):
        """Return the window to ordinary Tk painting (for fallback rendering)."""
        if self._active:
            style = self.user.GetWindowLongPtrW(self.hwnd, -20)
            self._style(style & ~self._LAYERED)
            self._active = False
            self._last = None

    def render(self, width: int, height: int, lines: Sequence[TextLine], *,
               background: str | None = None, force: bool = False):
        """Draw text; None background blends into the existing taskbar surface."""
        if width <= 0 or height <= 0:
            return
        lines = tuple(lines)
        key = (width, height, lines, background)
        self._enable()
        if not force and key == self._last:
            return
        pixels = self._pixels(width, height, lines, background)
        dc, bitmap, old_bitmap, bits = self._bitmap(width, height)
        try:
            ctypes.memmove(bits, bytes(pixels), len(pixels))
            size = wintypes.SIZE(width, height)
            source = wintypes.POINT(0, 0)
            blend = _Blend(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA.
            if not self.user.UpdateLayeredWindow(self.hwnd, None, None, ctypes.byref(size),
                                                 dc, ctypes.byref(source), 0,
                                                 ctypes.byref(blend), 2):  # ULW_ALPHA.
                raise ctypes.WinError(ctypes.get_last_error())
            self._last = key
        finally:
            self.gdi.SelectObject(dc, old_bitmap)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)

    def _bitmap(self, width: int, height: int):
        dc = self.gdi.CreateCompatibleDC(None)
        if not dc:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _BitmapInfo()
        info.header = _BitmapHeader(size=ctypes.sizeof(_BitmapHeader), width=width,
                                    height=-height, planes=1, bit_count=32,
                                    size_image=width * height * 4)
        bits = ctypes.c_void_p()
        bitmap = self.gdi.CreateDIBSection(dc, ctypes.byref(info), 0,
                                          ctypes.byref(bits), None, 0)
        if not bitmap:
            self.gdi.DeleteDC(dc)
            raise ctypes.WinError(ctypes.get_last_error())
        old_bitmap = self.gdi.SelectObject(dc, bitmap)
        return dc, bitmap, old_bitmap, bits

    def _pixels(self, width: int, height: int, lines: Sequence[TextLine],
                background: str | None) -> bytearray:
        if background is None:
            base = bytes((0, 0, 0, 1))
        else:
            red, green, blue = _rgb(background)
            base = bytes((blue, green, red, 255))
        pixels = bytearray(base * (width * height))
        dc, bitmap, old_bitmap, bits = self._bitmap(width, height)
        try:
            self.gdi.SetBkMode(dc, 1)  # TRANSPARENT.
            self.gdi.SetTextColor(dc, 0x00FFFFFF)
            for line in lines:
                if not line.text:
                    continue
                red, green, blue = _rgb(line.color)
                ctypes.memset(bits, 0, len(pixels))
                # ANTIALIASED_QUALITY produces grayscale coverage. ClearType's
                # RGB subpixels would cause colored fringes on transparency.
                font = self.gdi.CreateFontW(-max(1, line.size), 0, 0, 0,
                                            700 if line.bold else 400,
                                            0, 0, 0, 1, 0, 0, 4, 0, line.family)
                if not font:
                    raise ctypes.WinError(ctypes.get_last_error())
                old_font = self.gdi.SelectObject(dc, font)
                try:
                    count = len(line.text.encode("utf-16-le")) // 2
                    if not self.gdi.TextOutW(dc, line.x, line.y, line.text, count):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.gdi.GdiFlush()
                finally:
                    self.gdi.SelectObject(dc, old_font)
                    self.gdi.DeleteObject(font)
                mask = ctypes.string_at(bits, len(pixels))
                for offset in range(0, len(pixels), 4):
                    coverage = max(mask[offset:offset + 3])
                    if not coverage:
                        continue
                    inverse = 255 - coverage
                    for channel, value in enumerate((blue, green, red)):
                        pixels[offset + channel] = (value * coverage + pixels[offset + channel]
                                                     * inverse + 127) // 255
                    pixels[offset + 3] = coverage + (pixels[offset + 3] * inverse + 127) // 255
            return pixels
        finally:
            self.gdi.SelectObject(dc, old_bitmap)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)
