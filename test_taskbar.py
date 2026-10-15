"""Taskbar placement and widget lifecycle checks; no prices or network needed."""

import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gold_monitor.taskbar import TaskbarArea, TaskbarTicker, place_strip, initial_taskbar_area, _overlaps


def bottom_area(**changes):
    values = dict(rect=(0, 1032, 1920, 1080), monitor=(0, 0, 1920, 1080),
                  work=(0, 0, 1920, 1032), verified=True,
                  occupied=((0, 1032, 810, 1080), (1439, 1032, 1591, 1080),
                            (1603, 1032, 1920, 1080)))
    values.update(changes)
    return TaskbarArea(**values)


class PlacementTests(unittest.TestCase):
    def test_initial_fallback_is_inside_known_workarea(self):
        for work in ((0, 0, 1920, 1032), (0, 48, 1920, 1080),
                     (48, 0, 1920, 1080), (0, 0, 1872, 1080)):
            with self.subTest(work=work):
                area = initial_taskbar_area(1920, 1080, work)
                p = place_strip(area, 190, 38)
                self.assertFalse(area.hidden)
                self.assertFalse(p.in_taskbar)
                self.assertGreaterEqual(p.x, work[0])
                self.assertGreaterEqual(p.y, work[1])
                self.assertLessEqual(p.x + p.width, work[2])
                self.assertLessEqual(p.y + p.height, work[3])
                self.assertFalse(_overlaps((p.x, p.y, p.x + p.width, p.y + p.height), area.rect))

    def test_missing_or_autohide_workarea_waits_for_scan(self):
        for work in (None, (0, 0, 1920, 1080), (-1, 0, 1920, 1080)):
            self.assertTrue(initial_taskbar_area(1920, 1080, work).hidden)

    def test_free_gap_preserves_weather_and_notification_area(self):
        area = bottom_area()
        p = place_strip(area, 190, 38)
        self.assertTrue(p.in_taskbar)
        self.assertEqual((p.x, p.y), (1243, 1037))
        for occupied in area.occupied:
            self.assertFalse(_overlaps((p.x, p.y, p.x + p.width, p.y + p.height), occupied))

    def test_drag_is_clamped_to_a_safe_gap(self):
        area = bottom_area()
        self.assertEqual(place_strip(area, 190, 38, 1100).x, 1100)
        self.assertEqual(place_strip(area, 190, 38, 1700).x, 1243)
        self.assertEqual(place_strip(area, 190, 38, 300).x, 816)

    def test_full_taskbar_uses_adjacent_space(self):
        area = bottom_area(occupied=((0, 1032, 1920, 1080),))
        p = place_strip(area, 190, 38)
        self.assertFalse(p.in_taskbar)
        self.assertEqual(p.reason, "taskbar-full")
        self.assertLessEqual(p.y + p.height, area.work[3])

    def test_unknown_layout_never_covers_taskbar(self):
        p = place_strip(bottom_area(verified=False, occupied=()), 190, 38)
        self.assertFalse(p.in_taskbar)
        self.assertEqual(p.reason, "layout-unavailable")

    def test_top_and_vertical_taskbars(self):
        top = TaskbarArea((0, 0, 1920, 48), (0, 0, 1920, 1080), (0, 48, 1920, 1080))
        self.assertGreaterEqual(place_strip(top, 190, 38).y, 48)
        left = TaskbarArea((0, 0, 48, 1080), (0, 0, 1920, 1080), (48, 0, 1920, 1080), verified=True)
        p = place_strip(left, 190, 38)
        self.assertEqual(p.reason, "vertical-taskbar")
        self.assertGreaterEqual(p.x, 48)

    def test_negative_monitor_and_scaled_strip(self):
        area = TaskbarArea((-2360, 889, -1011, 937), (-2360, 0, -1011, 937),
                           (-2360, 0, -1011, 889),
                           ((-2360, 889, -1553, 937), (-1225, 889, -1011, 937)), True)
        p = place_strip(area, 190, 38)
        self.assertEqual((p.x, p.y), (-1421, 894))
        self.assertTrue(p.in_taskbar)
        # A high-DPI strip too tall for this taskbar must remain reachable.
        p = place_strip(area, 285, 57, -20000)
        self.assertFalse(p.in_taskbar)
        self.assertGreaterEqual(p.x, -2360)
        self.assertLessEqual(p.x + p.width, -1011)


class TickerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.restored = 0
        self.positions = []
        self.ticker = TaskbarTicker(self.root, self.restore,
                                    on_position=lambda x, y: self.positions.append((x, y)))
        self.worker_patch = patch.object(self.ticker, "_start_locator")
        self.worker_patch.start()
        self.desktop_patch = patch("gold_monitor.taskbar._suppress_for_desktop", return_value=False)
        self.desktop_check = self.desktop_patch.start()

    def tearDown(self):
        self.worker_patch.stop()
        self.desktop_patch.stop()
        self.ticker.destroy()
        self.root.destroy()

    def restore(self):
        self.restored += 1

    def publish(self, area):
        self.ticker._results.put((time.monotonic(), [area]))
        self.ticker.show()
        self.root.update()

    def test_show_update_hide_and_reopen(self):
        self.publish(bottom_area())
        self.assertTrue(self.ticker.visible)
        self.assertTrue(self.ticker.window.winfo_ismapped())
        self.assertTrue(self.ticker.in_taskbar)
        self.ticker.update_price("$4,216.30 /oz", "¥965.80 /g", color="#69d5b0", status="延迟")
        text = [self.ticker.canvas.itemcget(i, "text") for i in self.ticker.canvas.find_all()]
        self.assertEqual(text, ["$4,216.30 /oz", "¥965.80 /g  延迟"])
        self.ticker.hide()
        self.assertFalse(self.ticker.visible)
        self.assertFalse(self.ticker.window.winfo_ismapped())
        self.assertIsNone(self.ticker._job)
        self.ticker.show()
        self.root.update()
        self.assertTrue(self.ticker.window.winfo_ismapped())

    def test_first_show_does_not_cover_taskbar_before_scan(self):
        with patch("gold_monitor.taskbar._primary_workarea", return_value=(0, 0, 1920, 1032)), \
                patch.object(self.root, "winfo_screenwidth", return_value=1920), \
                patch.object(self.root, "winfo_screenheight", return_value=1080):
            self.ticker.show()
            self.root.update()
            self.assertFalse(self.ticker.in_taskbar)
            self.assertLessEqual(self.ticker.window.winfo_y() + self.ticker.window.winfo_height(), 1032)
        self.ticker.hide()
        with patch("gold_monitor.taskbar._primary_workarea", return_value=None):
            self.ticker.show()
            self.root.update()
            self.assertFalse(self.ticker.window.winfo_ismapped())

    def test_auto_hide_and_fullscreen_restore_automatically(self):
        self.publish(bottom_area(hidden=True))
        self.assertFalse(self.ticker.window.winfo_ismapped())
        self.assertTrue(self.ticker.visible)
        self.publish(bottom_area(fullscreen=True))
        self.assertFalse(self.ticker.window.winfo_ismapped())
        self.publish(bottom_area())
        self.assertTrue(self.ticker.window.winfo_ismapped())
        # Native suppression is checked even while the cached UIA layout says visible.
        self.desktop_check.return_value = True
        self.ticker._synchronize()
        self.assertFalse(self.ticker.window.winfo_ismapped())

    def test_click_restores_but_drag_saves_position(self):
        self.publish(bottom_area())
        original_x = self.ticker.window.winfo_x()
        e = SimpleNamespace(x_root=original_x + 8, y_root=1040)
        self.ticker._press(e)
        self.ticker._release(e)
        self.assertEqual(self.restored, 1)
        self.ticker._press(e)
        self.ticker._drag(SimpleNamespace(x_root=e.x_root - 100, y_root=1040))
        self.root.update()
        self.ticker._release(e)
        self.assertEqual(self.restored, 1)
        self.assertEqual(len(self.positions), 1)
        self.assertEqual(self.positions[0][0], original_x - 100)

    def test_stale_layout_falls_back_off_taskbar(self):
        self.publish(bottom_area())
        self.ticker._sampled = time.monotonic() - 10
        self.ticker._synchronize()
        self.assertFalse(self.ticker.in_taskbar)
        self.assertEqual(self.ticker.placement_reason, "layout-unavailable")

    def test_open_menu_holds_anchor_while_quotes_continue(self):
        self.publish(bottom_area())
        original = self.ticker._last_geometry
        anchor, work = self.ticker.begin_menu()
        self.assertEqual(anchor, original)
        self.assertEqual(work, bottom_area().work)
        # A delayed shell scan or a changed status must not move/re-raise the
        # strip while the user is aiming at the popup anchored above it.
        self.ticker._sampled = time.monotonic() - 10
        with patch.object(self.ticker, "_place") as place:
            self.ticker.update_price("$4,222.99 /oz", "¥950.12 /g")
            self.ticker._synchronize()
            place.assert_not_called()
        self.assertIn("4,222.99", self.ticker.primary)
        self.assertEqual(self.ticker._last_geometry, original)
        self.ticker.end_menu()
        self.ticker._synchronize()
        self.assertFalse(self.ticker.in_taskbar)

    def test_hiding_dismisses_menu_layout_hold(self):
        self.publish(bottom_area())
        self.ticker.begin_menu()
        self.ticker.hide()
        self.assertFalse(self.ticker.menu_open)


if __name__ == "__main__":
    unittest.main()
