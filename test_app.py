"""Native Tk integration checks; all quotes are synthetic and no network is used."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gold_monitor.app import GOLD, GREEN, RED, GoldMonitor
from gold_monitor.prices import FxQuote, GoldQuote, PriceSnapshot
from gold_monitor.settings import Settings, fit_position


WORKSPACE = Path(__file__).resolve().parent
TEST_STATE = WORKSPACE / ".test-state"


def sample_snapshot(*, price="3110.34768", gold_age=0, fx_age=0, errors=()):
    now = datetime.now(timezone.utc)
    return PriceSnapshot(
        GoldQuote(Decimal(price), now - timedelta(seconds=gold_age), now),
        FxQuote(Decimal("7"), now - timedelta(seconds=fx_age), now + timedelta(days=1), now),
        now,
        errors,
    )


class FakeClient:
    def __init__(self, result, *, blocked=False, source="swissquote"):
        self.result = result
        self.source = source
        self.minimum_interval_seconds = 30 if source == "goldapi" else 1
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.thread_id = None
        self.calls = 0
        if not blocked:
            self.release.set()

    def refresh(self):
        self.calls += 1
        self.thread_id = threading.get_ident()
        self.started.set()
        try:
            if not self.release.wait(5):
                raise TimeoutError("test worker was not released")
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        finally:
            self.finished.set()


class AppTests(unittest.TestCase):
    def setUp(self):
        TEST_STATE.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_STATE)
        self.state_dir = Path(self.temporary.name)
        self.apps = []
        self.clients = []
        self.callback_errors = []
        ticker_patch = patch("gold_monitor.taskbar.TaskbarTicker", autospec=True)
        self.ticker_factory = ticker_patch.start()
        self.addCleanup(ticker_patch.stop)

    def tearDown(self):
        for client in self.clients:
            client.release.set()
        for app in self.apps:
            if not app.closed:
                app.close()
        for client in self.clients:
            if client.started.is_set():
                self.assertTrue(client.finished.wait(2), "test worker failed to finish")
        self.temporary.cleanup()
        self.assertEqual(self.callback_errors, [], "Tk callback raised an exception")

    def make_app(self, result=None, *, blocked=False, mapped=False, source="swissquote"):
        client = FakeClient(result if result is not None else sample_snapshot(), blocked=blocked, source=source)
        self.clients.append(client)
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        try:
            app = GoldMonitor(root, self.state_dir, client)
        except BaseException:
            root.destroy()
            raise
        self.apps.append(app)
        if mapped:
            root.deiconify()
        root.update_idletasks()
        return app, client

    def pump_until(self, app, predicate, *, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "Tk event condition timed out")
            app.root.update()
            time.sleep(0.005)
        app.root.update_idletasks()

    def text(self, app):
        return "\n".join(
            app.canvas.itemcget(item, "text")
            for item in app.canvas.find_all()
            if app.canvas.type(item) == "text"
        )

    def fetch(self, app):
        app.refresh()
        self.pump_until(app, lambda: not app.busy)

    def test_fetch_is_background_work_and_tk_remains_responsive(self):
        app, client = self.make_app(blocked=True)
        app.refresh()
        self.assertTrue(client.started.wait(1))
        self.assertNotEqual(client.thread_id, threading.get_ident())
        heartbeat = []
        app.root.after(10, lambda: heartbeat.append(True))
        self.pump_until(app, lambda: bool(heartbeat))
        self.assertTrue(app.busy)
        self.assertIsNone(app.snapshot)
        app.refresh(manual=True)
        self.assertEqual(client.calls, 1, "refresh must not overlap an active request")
        client.release.set()
        self.pump_until(app, lambda: not app.busy)
        self.assertIn("$3,110.35", self.text(app))
        self.assertIn("≈ ¥700.00", self.text(app))

    def test_first_failure_shows_placeholders_and_connection_failure(self):
        failed = PriceSnapshot(None, None, datetime.now(timezone.utc), ("金价：test offline", "汇率：test offline"))
        app, _ = self.make_app(failed)
        self.fetch(app)
        text = self.text(app)
        self.assertIn("连接失败", text)
        self.assertIn("$ — —", text)
        self.assertIn("汇率暂无数据", text)
        self.assertNotIn("$3,110", text)
        self.assertNotIn("¥700", text)

    def test_stale_quote_retains_value_and_warns_in_both_layouts(self):
        snapshot = sample_snapshot(gold_age=600, fx_age=3 * 86400, errors=("金价：test offline",))
        app, _ = self.make_app(snapshot)
        self.fetch(app)
        text = self.text(app)
        self.assertIn("$3,110.35", text)
        self.assertIn("≈ ¥700.00", text)
        self.assertIn("旧报价", text)
        self.assertIn("汇率已过期", text)
        self.assertEqual(list(app.history), [], "stale quotes must not enter live session history")
        app.toggle_compact()
        app.root.update_idletasks()
        text = self.text(app)
        self.assertIn("旧报价", text)
        self.assertIn("汇率过期", text)
        self.assertIn("$3,110.35", text)

    def test_unexpected_worker_error_finishes_request_without_tk_exception(self):
        app, _ = self.make_app(RuntimeError("synthetic worker failure"))
        with self.assertLogs(level="ERROR"):
            self.fetch(app)
        self.assertFalse(app.busy)
        self.assertIn("synthetic worker failure", app.last_error)
        self.assertIn("连接失败", self.text(app))

    def test_worker_failure_retains_quote_and_later_recovery_clears_error(self):
        app, client = self.make_app()
        self.fetch(app)
        original = app.snapshot
        client.result = RuntimeError("synthetic later failure")
        app.last_request -= 31
        with self.assertLogs(level="ERROR"):
            self.fetch(app)
        self.assertIs(app.snapshot, original)
        self.assertIn("连接异常", self.text(app))
        self.assertIn("保留上次报价", self.text(app))
        self.assertIn("$3,110.35", self.text(app))
        client.result = sample_snapshot()
        app.last_request -= 31
        self.fetch(app)
        self.assertIs(app.snapshot, client.result)
        self.assertEqual(app.last_error, "")
        self.assertIn("实时", self.text(app))

    def test_manual_refresh_respects_provider_one_second_minimum(self):
        app, client = self.make_app()
        self.fetch(app)
        app.refresh(manual=True)
        self.assertEqual(client.calls, 1)
        self.assertIn("秒后可刷新", self.text(app))
        app.last_request -= 2
        self.fetch(app)
        self.assertEqual(client.calls, 2)

    def test_gold_api_retains_thirty_second_minimum(self):
        app, client = self.make_app(source="goldapi")
        self.fetch(app)
        app.last_request -= 2
        app.refresh(manual=True)
        self.assertEqual(client.calls, 1)
        self.assertIn("秒后可刷新", self.text(app))
        app.last_request -= 29
        self.fetch(app)
        self.assertEqual(client.calls, 2)

    def test_close_does_not_wait_for_blocked_worker_or_leave_timers(self):
        app, client = self.make_app(blocked=True)
        app.refresh()
        self.assertTrue(client.started.wait(1))
        app.minimize_to_taskbar()
        ticker = app.ticker
        # Run both recurring callbacks before closing, so cancellation covers rescheduling.
        started = time.monotonic()
        self.pump_until(app, lambda: time.monotonic() - started > 1.1)
        started = time.monotonic()
        app.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(app.closed)
        ticker.destroy.assert_called_once_with()
        self.assertFalse(client.finished.is_set())
        self.assertEqual(app.root.tk.call("after", "info"), "")
        client.release.set()
        self.assertTrue(client.finished.wait(1))

    def test_unchanged_prices_never_create_an_animation(self):
        app, client = self.make_app()
        self.fetch(app)
        self.assertEqual(app._price_color(), GOLD)
        self.assertEqual(app.price_direction, 0)
        self.assertIsNone(app.flash_job)
        client.result = sample_snapshot()
        app.last_request -= 2
        self.fetch(app)
        self.assertEqual(app._price_color(), GOLD)
        self.assertEqual(app.price_direction, 0)
        self.assertIsNone(app.flash_job)

    def test_genuine_price_changes_flash_in_both_directions_then_expire(self):
        app, client = self.make_app()
        self.fetch(app)
        client.result = sample_snapshot(price="3111")
        app.last_request -= 2
        self.fetch(app)
        self.assertEqual(app.price_direction, 1)
        self.assertEqual(app._price_color(), GREEN)
        self.assertIn("$3,111.00", self.text(app))
        self.assertIsNotNone(app.flash_job)
        self.pump_until(app, lambda: app._price_color() == GOLD)
        client.result = sample_snapshot(price="3109")
        app.last_request -= 2
        self.fetch(app)
        self.assertEqual(app.price_direction, -1)
        self.assertEqual(app._price_color(), RED)
        self.assertIn("$3,109.00", self.text(app))

    def test_old_quote_does_not_flash_or_enter_session_history(self):
        app, client = self.make_app()
        self.fetch(app)
        history = list(app.history)
        client.result = sample_snapshot(price="3100", gold_age=600)
        app.last_request -= 2
        self.fetch(app)
        self.assertEqual(app._price_color(), GOLD)
        self.assertEqual(app.price_direction, 0)
        self.assertEqual(list(app.history), history)

    def test_taskbar_mode_keeps_refreshing_and_restores_floating_position(self):
        app, client = self.make_app(mapped=True)
        self.fetch(app)
        position = (app.root.winfo_x(), app.root.winfo_y())
        app.minimize_to_taskbar()
        ticker = app.ticker
        self.assertTrue(app.docked)
        self.assertEqual(app.root.state(), "withdrawn")
        ticker.show.assert_called_once_with()
        self.assertIn("$3,110.35", ticker.update_price.call_args.args[0])
        self.assertIn("/oz", ticker.update_price.call_args.args[0])
        self.assertIn("¥700.00", ticker.update_price.call_args.args[1])
        client.result = sample_snapshot(price="3111")
        app.last_request -= 2
        app.next_refresh = 0
        self.pump_until(app, lambda: client.calls >= 2 and not app.busy)
        self.assertIn("$3,111.00", ticker.update_price.call_args.args[0])
        self.assertEqual((app.settings.x, app.settings.y), position)
        self.assertEqual(app.root.state(), "withdrawn")
        # The ticker calls the supplied restore callback when the user clicks it.
        self.ticker_factory.call_args.kwargs["on_restore"]()
        app.root.update_idletasks()
        self.assertFalse(app.docked)
        self.assertFalse(app.settings.docked)
        ticker.hide.assert_called_once_with()
        self.assertEqual((app.root.winfo_x(), app.root.winfo_y()), position)
        app.minimize_to_taskbar()
        self.ticker_factory.assert_called_once()
        app._save_taskbar_position(-400, 1040)
        app.close()
        saved = Settings.load(app.settings_path)
        self.assertTrue(saved.docked)
        self.assertEqual((saved.x, saved.y), position)
        self.assertEqual((saved.taskbar_x, saved.taskbar_y), (-400, 1040))

    def test_only_native_root_minimize_enters_taskbar_mode(self):
        app, _ = self.make_app()
        app._on_unmap(SimpleNamespace(widget=app.root))
        app.root.update_idletasks()
        self.assertFalse(app.docked, "ordinary withdraw must not trigger taskbar mode")
        with patch.object(app.root, "state", return_value="iconic"):
            app._on_unmap(SimpleNamespace(widget=app.canvas))
            app.root.update_idletasks()
            self.assertFalse(app.docked, "child-widget unmap must not trigger taskbar mode")
            app._on_unmap(SimpleNamespace(widget=app.root))
        app.root.update_idletasks()
        self.assertTrue(app.docked)
        self.ticker_factory.return_value.show.assert_called_once_with()

    def test_taskbar_menu_does_not_block_price_results_and_restore_closes_it(self):
        app, client = self.make_app(blocked=True)
        app.minimize_to_taskbar()
        app.ticker.begin_menu.return_value = ((100, 900, 110, 32), (0, 0, 1920, 900))
        app.ticker.light_theme = True
        app.refresh()
        self.assertTrue(client.started.wait(1))
        app.show_menu(SimpleNamespace(x_root=110, y_root=910))
        menu = app.context_menu
        self.assertIsNotNone(menu)
        client.release.set()
        self.pump_until(app, lambda: not app.busy)
        self.assertIn("$3,110.35", app.ticker.update_price.call_args.args[0])
        self.assertIs(app.context_menu, menu)
        app.restore_window()
        self.assertIsNone(app.context_menu)
        self.assertFalse(menu.window.winfo_exists())
        self.assertIsNone(app.root.grab_current())
        app.ticker.end_menu.assert_called_once_with()

    def test_repeated_native_menus_do_not_accumulate_tk_widgets(self):
        app, _ = self.make_app()
        original = app.root.winfo_children()
        with patch.object(tk.Menu, "tk_popup"):
            for _ in range(20):
                app.show_menu(SimpleNamespace(x_root=100, y_root=100))
        self.assertEqual(app.root.winfo_children(), original)

    def test_close_cancels_pending_native_minimize_callback(self):
        app, _ = self.make_app()
        with patch.object(app.root, "state", return_value="iconic"):
            app._on_unmap(SimpleNamespace(widget=app.root))
        app.close()
        self.assertEqual(app.root.tk.call("after", "info"), "")
        self.ticker_factory.assert_not_called()

    def test_source_change_resets_session_and_respects_new_minimum(self):
        app, _ = self.make_app()
        self.fetch(app)
        replacement = FakeClient(sample_snapshot(price="3200"), blocked=True, source="goldapi")
        self.clients.append(replacement)
        with patch("gold_monitor.app.PriceClient", return_value=replacement) as factory:
            app.set_source("goldapi")
            factory.assert_called_once_with(timeout=5, source="goldapi")
            self.assertTrue(replacement.started.wait(1))
            self.assertIsNone(app.snapshot)
            self.assertIsNone(app.session_open)
            self.assertEqual(list(app.history), [])
            self.assertEqual(app.price_direction, 0)
            self.assertEqual(app.settings.interval, 30)
            self.assertEqual(app.minimum_interval, 30)
            replacement.release.set()
            self.pump_until(app, lambda: not app.busy)
        self.assertEqual(app.session_open, Decimal("3200"))
        self.assertEqual(len(app.history), 1)
        self.assertIn("+0.00", app._session_change())
        self.assertEqual(app._price_color(), GOLD)

    def test_source_change_during_fetch_waits_then_discards_previous_session(self):
        app, original = self.make_app(blocked=True)
        app.refresh()
        self.assertTrue(original.started.wait(1))
        replacement = FakeClient(sample_snapshot(price="3200"), blocked=True, source="goldapi")
        self.clients.append(replacement)
        with patch("gold_monitor.app.PriceClient", return_value=replacement) as factory:
            app.set_source("goldapi")
            factory.assert_not_called()
            self.assertIs(app.client, original)
            original.release.set()
            self.pump_until(app, lambda: app.client is replacement)
            self.assertTrue(replacement.started.wait(1))
            self.assertTrue(app.busy)
            self.assertIsNone(app.snapshot)
            self.assertIsNone(app.session_open)
            self.assertEqual(list(app.history), [])
            replacement.release.set()
            self.pump_until(app, lambda: not app.busy)
        self.assertEqual(app.snapshot.usd_per_oz, Decimal("3200"))
        self.assertEqual(app.session_open, Decimal("3200"))
        self.assertIsNone(app.pending_source)

    def test_compact_geometry_and_settings_restore(self):
        app, _ = self.make_app(mapped=True)
        app.toggle_compact()
        app.toggle_pin()
        app.set_interval(60)
        app.set_opacity(0.9)
        app.root.update_idletasks()
        geometry = app.root.geometry().split("+", 1)[0]
        self.assertEqual(geometry, f"{app.px(app.WIDTH)}x{app.px(app.COMPACT_HEIGHT)}")
        position = (app.root.winfo_x(), app.root.winfo_y())
        app.close()
        restored, _ = self.make_app(mapped=True)
        self.assertTrue(restored.settings.compact)
        self.assertFalse(restored.settings.topmost)
        self.assertEqual(restored.settings.interval, 60)
        self.assertEqual(restored.settings.opacity, 0.9)
        self.assertEqual((restored.root.winfo_x(), restored.root.winfo_y()), position)
        self.assertEqual(restored.root.geometry().split("+", 1)[0], geometry)

    def test_saved_negative_monitor_position_is_preserved(self):
        Settings(x=-1100, y=80).save(self.state_dir / "settings.json")
        with patch("gold_monitor.app.monitor_workareas", return_value=[(-1920, 0, 0, 1080), (0, 0, 1920, 1080)]):
            app, _ = self.make_app(mapped=True)
            self.assertEqual((app.root.winfo_x(), app.root.winfo_y()), (-1100, 80))


class SettingsTests(unittest.TestCase):
    def test_invalid_and_unreadable_settings_do_not_prevent_startup(self):
        TEST_STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_STATE) as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(Settings.load(path), Settings())
            for contents in ("{", "[]", "null", '{"interval": true, "x": false, "opacity": "0.9", "compact": 1}'):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    self.assertEqual(Settings.load(path), Settings())

    def test_valid_settings_round_trip(self):
        TEST_STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_STATE) as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(x=-1000, y=150, interval=120, topmost=False, compact=True, opacity=0.8)
            self.assertTrue(settings.save(path))
            self.assertEqual(Settings.load(path), settings)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["x"], -1000)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_new_and_legacy_settings_enable_one_second_feed(self):
        self.assertEqual(Settings().source, "swissquote")
        self.assertEqual(Settings().interval, 1)
        TEST_STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_STATE) as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"interval": 30, "x": -1000, "y": 80, "compact": true}', encoding="utf-8")
            settings = Settings.load(path)
            self.assertEqual((settings.source, settings.interval), ("swissquote", 1))
            self.assertEqual((settings.x, settings.y, settings.compact), (-1000, 80, True))

    def test_taskbar_position_and_feed_settings_round_trip(self):
        TEST_STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_STATE) as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(x=250, y=100, interval=2, docked=True, taskbar_x=-500, taskbar_y=1040)
            self.assertTrue(settings.save(path))
            self.assertEqual(Settings.load(path), settings)
            settings.source = "goldapi"
            settings.interval = 1
            self.assertTrue(settings.save(path))
            self.assertEqual(Settings.load(path).interval, 30)

    def test_supported_feed_intervals_are_preserved(self):
        TEST_STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_STATE) as directory:
            path = Path(directory) / "settings.json"
            for interval in (1, 2, 5, 30, 60, 120):
                with self.subTest(interval=interval):
                    settings = Settings(source="swissquote", interval=interval)
                    settings.save(path)
                    self.assertEqual(Settings.load(path).interval, interval)

    def test_negative_monitor_origin_and_disconnected_monitor_recovery(self):
        self.assertEqual(fit_position(-1100, 80, 374, 268, [(-1920, 0, 0, 1080), (0, 0, 1920, 1080)]), (-1100, 80))
        self.assertEqual(fit_position(-1100, 80, 374, 268, [(0, 0, 1920, 1080)]), (0, 80))
        self.assertEqual(fit_position(1900, 1050, 374, 268, [(0, 0, 1920, 1080)]), (1546, 812))


if __name__ == "__main__":
    unittest.main()
