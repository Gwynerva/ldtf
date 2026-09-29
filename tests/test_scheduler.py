"""Scheduling of automatic syncs and the settings behind it (offline)."""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.appsettings import load_app_settings, save_app_settings  # noqa: E402
from dtf_backup.scheduler import RETRY, human_when, next_run  # noqa: E402
from dtf_backup.settings import load_settings, save_settings  # noqa: E402


def ts(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi).timestamp()


class NextRunTest(unittest.TestCase):
    def test_off(self) -> None:
        self.assertIsNone(next_run({"schedule": "off"}, ts(2026, 9, 1), None, ts(2026, 9, 2)))

    def test_interval(self) -> None:
        s = {"schedule": "interval", "schedule_hours": 6}
        self.assertEqual(next_run(s, ts(2026, 9, 1, 10), None, ts(2026, 9, 1, 11)), ts(2026, 9, 1, 16))
        self.assertEqual(next_run(s, None, None, 1000.0), 1000.0)   # never synced: now

    def test_daily_and_catch_up(self) -> None:
        s = {"schedule": "daily", "schedule_time": "04:00"}
        # synced yesterday 04:10 -> due today 04:00
        self.assertEqual(next_run(s, ts(2026, 9, 1, 4, 10), None, ts(2026, 9, 2, 1)), ts(2026, 9, 2, 4))
        # the computer was off at 04:00: at 09:00 the run is overdue (in the past = run now)
        self.assertLess(next_run(s, ts(2026, 9, 1, 4, 10), None, ts(2026, 9, 2, 9)), ts(2026, 9, 2, 9))
        # after that catch-up run at 09:05 the next one is tomorrow 04:00
        self.assertEqual(next_run(s, ts(2026, 9, 2, 9, 5), None, ts(2026, 9, 2, 10)), ts(2026, 9, 3, 4))
        # synced at 03:00 -> the same day's 04:00
        self.assertEqual(next_run(s, ts(2026, 9, 2, 3), None, ts(2026, 9, 2, 3, 30)), ts(2026, 9, 2, 4))

    def test_retry_backoff_after_failures(self) -> None:
        s = {"schedule": "interval", "schedule_hours": 1}
        last_ok, fail_at = ts(2026, 9, 1, 0), ts(2026, 9, 1, 5)
        self.assertEqual(next_run(s, last_ok, {"ts": fail_at, "ok": False, "fails": 1}, fail_at), fail_at + RETRY[0])
        self.assertEqual(next_run(s, last_ok, {"ts": fail_at, "ok": False, "fails": 3}, fail_at), fail_at + RETRY[2])
        self.assertEqual(next_run(s, last_ok, {"ts": fail_at, "ok": False, "fails": 99}, fail_at), fail_at + RETRY[-1])
        self.assertEqual(next_run(s, last_ok, {"ts": fail_at, "ok": True, "fails": 0}, fail_at), last_ok + 3600)

    def test_human_when(self) -> None:
        now = ts(2026, 9, 28, 12)
        self.assertEqual(human_when(ts(2026, 9, 28, 16, 30), now), "сегодня в 16:30")
        self.assertEqual(human_when(ts(2026, 9, 29, 4), now), "завтра в 04:00")
        self.assertEqual(human_when(ts(2026, 10, 3, 4), now), "3 октября в 04:00")
        self.assertEqual(human_when(now - 5, now), "скоро")


class SettingsTest(unittest.TestCase):
    def test_migrate_auto_sync_hours(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({"auto_sync_hours": 6, "workers": 3}), encoding="utf-8")
            s = load_settings(p)
            self.assertEqual((s["schedule"], s["schedule_hours"], s["scope"]), ("interval", 6, "all"))
            self.assertNotIn("workers", s)   # network knobs are not archive settings any more
            p.write_text(json.dumps({"auto_sync_hours": 0}), encoding="utf-8")
            self.assertEqual(load_settings(p)["schedule"], "off")

    def test_form_keeps_hidden_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            save_settings(p, {"refresh_days": 7, "media": "posts", "scope": "posts"})
            s = save_settings(p, {"schedule": "daily", "schedule_time": "5:07", "schedule_hours": "999"})
            self.assertEqual((s["schedule"], s["schedule_time"], s["schedule_hours"]), ("daily", "05:07", 168))
            # fields the form doesn't have (refresh_days: settings.json / CLI only) and radios keep their values
            self.assertEqual((s["refresh_days"], s["media"], s["scope"]), (7, "posts", "posts"))
            self.assertEqual(save_settings(p, {"scope": "everything"})["scope"], "posts")   # unknown choice ignored
            s = save_settings(p, {"schedule": "weekly", "schedule_time": "25:00"})
            self.assertEqual((s["schedule"], s["schedule_time"]), ("daily", "05:07"))   # invalid values ignored

    def test_app_settings(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            self.assertEqual(load_app_settings(lib), {"autosync": True, "notify": True, "open_browser": True})
            # values of older versions (the network budget was a setting) are ignored, not an error
            s = save_app_settings(lib, {"max_parallel": "9", "api_rate": "0.2", "autosync": "1", "notify": "1"})
            self.assertEqual(s, {"autosync": True, "notify": True, "open_browser": False})
            s = save_app_settings(lib, {"autosync": False}, partial=True)   # tray toggle keeps the rest
            self.assertEqual((s["autosync"], s["notify"]), (False, True))


if __name__ == "__main__":
    unittest.main()
