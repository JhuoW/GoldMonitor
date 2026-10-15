"""A nonblocking, single-pane context menu for the taskbar quote strip."""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Sequence


@dataclass(frozen=True)
class MenuItem:
    label: str
    command: Callable[[], None] | None = None
    children: Sequence["MenuItem"] = ()
    accelerator: str = ""
    checked: bool = False


def _position_popup(anchor, work_area, width, height, gap=6):
    """Return an absolute screen position, preferring space away from the strip."""
    x, y, aw, ah = anchor
    left, top, right, bottom = work_area
    clamp_x = lambda value: max(left, min(value, right - width))
    clamp_y = lambda value: max(top, min(value, bottom - height))
    candidates = {
        "above": (clamp_x(x), y - gap - height),
        "below": (clamp_x(x), y + ah + gap),
        "right": (x + aw + gap, clamp_y(y)),
        "left": (x - gap - width, clamp_y(y)),
    }
    if y + ah > bottom:
        order = ("above", "below", "left", "right")
    elif y < top:
        order = ("below", "above", "left", "right")
    elif x < left:
        order = ("right", "left", "above", "below")
    elif x + aw > right:
        order = ("left", "right", "above", "below")
    elif y - top >= bottom - y - ah:
        order = ("above", "below", "left", "right")
    else:
        order = ("below", "above", "left", "right")
    for side in order:
        px, py = candidates[side]
        if left <= px <= right - width and top <= py <= bottom - height:
            return px, py
    # Tiny work areas may have no nonoverlapping placement; always stay visible.
    px, py = candidates[order[0]]
    return clamp_x(px), clamp_y(py)


class PopupMenu:
    """A focusable popup without tk_popup's nested native menu message loop.

    Blank labels are separators. Submenus replace this pane and provide a Back
    row, so neither a changing quote nor a wide submenu can push menus offscreen.
    """

    def __init__(self, root: tk.Misc, items: Sequence[MenuItem], *,
                 anchor: tuple[int, int, int, int],
                 work_area: tuple[int, int, int, int], scale: float = 1.0,
                 light: bool = True, on_close: Callable[[], None] | None = None):
        self.root = root
        self.anchor = anchor
        self.work_area = work_area
        self.scale = max(0.5, float(scale))
        self.on_close = on_close
        self._pages = [(tuple(items), "")]
        self._closed = False
        self._shown = False
        self._focused = False
        self._jobs: set[str] = set()
        self._selection = -1
        self._scroll = 0
        self._rows: list[tuple[MenuItem, int, int]] = []
        self._colors = ({"bg": "#f7f9fc", "fg": "#253346", "muted": "#657387",
                         "hover": "#e3eaf4", "line": "#ccd5e0", "accent": "#08784f"}
                        if light else
                        {"bg": "#252c36", "fg": "#edf2f7", "muted": "#b0bbc9",
                         "hover": "#3b4758", "line": "#4b5869", "accent": "#69d5b0"})
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title("Gold Monitor · 任务栏菜单")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=self._colors["line"])
        self.font = tkfont.Font(self.window, family="Microsoft YaHei UI",
                                size=-max(10, round(13 * self.scale)))
        self.small_font = tkfont.Font(self.window, family="Segoe UI",
                                      size=-max(9, round(12 * self.scale)))
        self._pad = max(3, round(4 * self.scale))
        self._inset = max(8, round(10 * self.scale))
        self._mark = max(15, round(20 * self.scale))
        self._row_height = max(round(29 * self.scale), self.font.metrics("linespace") + 10)
        self.canvas = tk.Canvas(self.window, bd=0, highlightthickness=0,
                                bg=self._colors["bg"], takefocus=True)
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", self._leave)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.window.bind("<ButtonPress>", self._click)
        self.window.bind("<KeyPress>", self._key)
        self.window.bind("<FocusIn>", self._focus_in)
        self.window.bind("<FocusOut>", self._focus_out)
        self.window.bind("<Destroy>", self._destroyed)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

    def _later(self, callback):
        if self._closed:
            return
        holder = []

        def run():
            self._jobs.discard(holder[0])
            if not self._closed:
                callback()

        holder.append(self.root.after_idle(run))
        self._jobs.add(holder[0])

    def show(self):
        if self._closed or self._shown:
            return
        self._shown = True
        try:
            self._layout()
            self.window.deiconify()
            self.window.lift()
            self._later(self._activate)
        except tk.TclError:
            self.close()
            raise

    def _activate(self):
        # A local grab routes clicks outside our pane to _click, while other apps
        # still receive their input normally and dismiss us via focus loss.
        try:
            self.window.focus_force()
            self.window.grab_set()
        except tk.TclError:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for job in tuple(self._jobs):
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        try:
            if self.window.grab_current() == self.window:
                self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()

    def _destroyed(self, event):
        if event.widget == self.window:
            self.close()

    def _layout(self):
        items, title = self._pages[-1]
        displayed = ((MenuItem("返回"),) + items) if len(self._pages) > 1 else items
        self._rows = []
        y = self._pad
        desired_width = round(204 * self.scale)
        for index, item in enumerate(displayed):
            height = self._row_height if item.label else max(7, round(9 * self.scale))
            self._rows.append((item, y, height))
            y += height
            label = f"返回   {title}" if title and index == 0 else item.label
            label_width = self.font.measure(label)
            tail_width = self.small_font.measure(item.accelerator)
            desired_width = max(desired_width, label_width + tail_width +
                                2 * self._inset + 2 * self._mark + self._pad * 2)
        left, top, right, bottom = self.work_area
        self._width = min(desired_width + 2, max(1, right - left))
        self._content_height = y + self._pad
        self._height = min(self._content_height + 2, max(1, bottom - top))
        self._scroll = 0
        self._selection = next((i for i in range(len(self._rows)) if self._selectable(i)), -1)
        x, y = _position_popup(self.anchor, self.work_area, self._width,
                               self._height, max(4, round(6 * self.scale)))
        # '+-123' means absolute negative coordinates, unlike '-123', which Tk
        # interprets as an offset from the screen's right or bottom edge.
        self.window.geometry(f"{self._width}x{self._height}+{x}+{y}")
        self._draw()

    def _selectable(self, index):
        item = self._rows[index][0]
        return bool(item.label and (item.command or item.children or
                                    (index == 0 and len(self._pages) > 1)))

    def _fit(self, label, width, font):
        if font.measure(label) <= width:
            return label
        while label and font.measure(label + "…") > width:
            label = label[:-1]
        return label + "…" if label else ""

    def _draw(self):
        self.canvas.delete("all")
        palette = self._colors
        canvas_width = self._width - 2
        for index, (item, top, height) in enumerate(self._rows):
            y = top - self._scroll
            if y + height < 0 or y > self._height:
                continue
            if not item.label:
                self.canvas.create_line(self._inset, y + height // 2,
                                        canvas_width - self._inset, y + height // 2,
                                        fill=palette["line"])
                continue
            if index == self._selection:
                self.canvas.create_rectangle(self._pad, y, canvas_width - self._pad,
                                             y + height, fill=palette["hover"], outline="")
            cy = y + height // 2
            back = index == 0 and len(self._pages) > 1
            mark = "‹" if back else "✓" if item.checked else ""
            if mark:
                self.canvas.create_text(self._inset + self._mark // 2, cy, text=mark,
                                        fill=palette["accent"], font=self.font)
            color = palette["fg"] if self._selectable(index) else palette["muted"]
            label = f"返回   {self._pages[-1][1]}" if back else item.label
            tail = "›" if item.children else item.accelerator
            tail_width = self.small_font.measure(tail)
            label_room = canvas_width - 2 * self._inset - self._mark - tail_width
            if tail:
                label_room -= self._mark
            self.canvas.create_text(self._inset + self._mark, cy, anchor="w",
                                    text=self._fit(label, label_room, self.font),
                                    fill=color, font=self.font)
            if tail:
                self.canvas.create_text(canvas_width - self._inset, cy, anchor="e",
                                        text=tail, fill=palette["muted"], font=self.small_font)

    def _index_at(self, x, y):
        if not (0 <= x < self._width - 2 and 0 <= y < self._height - 2):
            return -1
        for index, (_, top, height) in enumerate(self._rows):
            if top <= y + self._scroll < top + height and self._selectable(index):
                return index
        return -1

    def _motion(self, event):
        index = self._index_at(event.x, event.y)
        if index != self._selection:
            self._selection = index
            self._draw()

    def _leave(self, _event):
        self._selection = -1
        self._draw()

    def _click(self, event):
        x = event.x_root - self.window.winfo_rootx()
        y = event.y_root - self.window.winfo_rooty()
        if not (0 <= x < self._width and 0 <= y < self._height):
            self.close()
        elif event.num == 1:
            index = self._index_at(x - 1, y - 1)
            if index >= 0:
                self._activate_row(index)
        return "break"

    def _activate_row(self, index):
        if index < 0 or not self._selectable(index):
            return
        if index == 0 and len(self._pages) > 1:
            self._back()
            return
        item = self._rows[index][0]
        if item.children:
            self._pages.append((tuple(item.children), item.label))
            self._layout()
        elif item.command:
            self._run(item.command)

    def _run(self, command):
        self.close()
        try:
            self.root.after_idle(command)
        except tk.TclError:
            pass

    def _back(self):
        if len(self._pages) > 1:
            self._pages.pop()
            self._layout()

    def _move_selection(self, step):
        choices = [i for i in range(len(self._rows)) if self._selectable(i)]
        if not choices:
            return
        current = choices.index(self._selection) if self._selection in choices else (-1 if step > 0 else 0)
        self._selection = choices[(current + step) % len(choices)]
        _, top, height = self._rows[self._selection]
        visible_height = self._height - 2
        if top < self._scroll:
            self._scroll = top
        elif top + height > self._scroll + visible_height:
            self._scroll = top + height - visible_height
        self._draw()

    def _key(self, event):
        key = event.keysym
        state = getattr(event, "state", 0)
        modifiers = set()
        if state & 0x4:
            modifiers.add("ctrl")
        if state & 0x1:
            modifiers.add("shift")
        if state & (0x8 | 0x20000):
            modifiers.add("alt")
        # Shortcuts refer to direct root actions even while browsing a submenu.
        for item in self._pages[0][0]:
            parts = item.accelerator.lower().split("+")
            if (item.command and item.accelerator and parts[-1] == key.lower()
                    and set(parts[:-1]) == modifiers):
                self._run(item.command)
                return "break"
        if key == "Escape":
            if len(self._pages) > 1:
                self._back()
            else:
                self.close()
        elif key in ("Up", "Down"):
            self._move_selection(-1 if key == "Up" else 1)
        elif key in ("Return", "KP_Enter", "space"):
            self._activate_row(self._selection)
        elif key == "Right" and self._selection >= 0:
            if self._rows[self._selection][0].children:
                self._activate_row(self._selection)
        elif key in ("Left", "BackSpace"):
            self._back()
        return "break"

    def _wheel(self, event):
        limit = max(0, self._content_height - self._height + 2)
        step = -self._row_height if event.delta > 0 else self._row_height
        self._scroll = max(0, min(limit, self._scroll + step))
        self._selection = -1
        self._draw()
        return "break"

    def _focus_in(self, _event):
        self._focused = True

    def _focus_out(self, _event):
        if self._focused:
            self._later(self._check_focus)

    def _check_focus(self):
        try:
            focused = self.window.focus_get()
            if focused is None or focused.winfo_toplevel() != self.window:
                self.close()
        except tk.TclError:
            self.close()
