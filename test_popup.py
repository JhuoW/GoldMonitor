"""Popup boundary and event lifecycle regressions; no network or native menu loops."""

from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from gold_monitor.popup import MenuItem, PopupMenu, _position_popup


class PopupPlacementTests(unittest.TestCase):
    def test_taskbars_on_each_edge_keep_menu_in_work_area_and_off_strip(self):
        cases = [
            ((1800, 1040, 104, 32), (0, 0, 1920, 1032)),
            ((1800, 4, 104, 32), (0, 48, 1920, 1080)),
            ((4, 900, 32, 104), (48, 0, 1920, 1080)),
            ((1884, 900, 32, 104), (0, 0, 1872, 1080)),
            ((-160, 1040, 104, 32), (-1920, 0, 0, 1032)),
        ]
        for anchor, work in cases:
            with self.subTest(anchor=anchor):
                width, height = 240, 320
                x, y = _position_popup(anchor, work, width, height)
                self.assertGreaterEqual(x, work[0])
                self.assertGreaterEqual(y, work[1])
                self.assertLessEqual(x + width, work[2])
                self.assertLessEqual(y + height, work[3])
                ax, ay, aw, ah = anchor
                overlaps = x < ax + aw and x + width > ax and y < ay + ah and y + height > ay
                self.assertFalse(overlaps)


class PopupLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.closed = Mock()
        self.ran = Mock()
        self.popup = PopupMenu(
            self.root,
            [MenuItem("刷新", self.ran, accelerator="F5"),
             MenuItem("来源", children=(MenuItem("更长的来源名称", self.ran, checked=True),)),
             MenuItem(""), MenuItem("退出", self.ran)],
            anchor=(-110, 1040, 100, 32), work_area=(-1920, 0, 0, 1032),
            on_close=self.closed)
        # Root integration tests exercise the native focus/grab path. These
        # layout tests stay withdrawn and do not take the desktop's focus.
        self.popup._layout()

    def tearDown(self):
        self.popup.close()
        self.root.destroy()

    def test_command_closes_once_and_runs_on_next_idle(self):
        self.popup._activate_row(0)
        self.closed.assert_called_once()
        self.ran.assert_not_called()
        self.popup.close()
        self.closed.assert_called_once()
        self.root.update_idletasks()
        self.ran.assert_called_once()

    def test_submenu_and_back_are_single_pane_and_stay_in_bounds(self):
        original_window = self.popup.window
        self.popup._activate_row(1)
        self.assertIs(self.popup.window, original_window)
        self.assertEqual(len(self.popup._pages), 2)
        self.assertFalse(self.popup._closed)
        self.assertIn("+-", self.popup.window.geometry())
        self.popup._key(SimpleNamespace(keysym="Escape", state=0))
        self.assertEqual(len(self.popup._pages), 1)
        self.assertFalse(self.popup._closed)
        self.popup._key(SimpleNamespace(keysym="Escape", state=0))
        self.assertTrue(self.popup._closed)
        self.closed.assert_called_once()

    def test_keyboard_skips_separator_and_accelerator_closes_before_action(self):
        self.popup._key(SimpleNamespace(keysym="Down", state=0))
        self.assertEqual(self.popup._selection, 1)
        self.popup._key(SimpleNamespace(keysym="Down", state=0))
        self.assertEqual(self.popup._selection, 3)
        self.popup._key(SimpleNamespace(keysym="F5", state=0))
        self.assertTrue(self.popup._closed)
        self.ran.assert_not_called()
        self.root.update_idletasks()
        self.ran.assert_called_once()

    def test_focus_leaving_closes_and_cancels_pending_work(self):
        self.popup._later(self.ran)
        with patch.object(self.popup.window, "focus_get", return_value=None):
            self.popup._check_focus()
        self.root.update_idletasks()
        self.closed.assert_called_once()
        self.ran.assert_not_called()


if __name__ == "__main__":
    unittest.main()
