"""Automatic sync of the archives: when each one is due (pure functions) and the app's scheduler thread.

Per archive (settings.json): `schedule` = off | interval (every `schedule_hours`) | daily (at `schedule_time`,
local time). A run missed while the computer was off or asleep happens as soon as the app is up again.
After a failed sync (network, DTF limits) the next try waits 30 min, 1 h, 2 h, 4 h, 8 h instead of hammering.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Callable

from .util import log

RETRY = (30 * 60, 60 * 60, 2 * 3600, 4 * 3600, 8 * 3600)
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def next_run(s: dict, last_ok: float | None, attempt: dict | None, now: float) -> float | None:
    """Timestamp when the archive is due (may be in the past = run now); None when the schedule is off."""
    mode = s.get("schedule")
    if mode == "interval":
        due = (last_ok + float(s.get("schedule_hours") or 12) * 3600) if last_ok else now
    elif mode == "daily":
        hh, mm = (int(x) for x in str(s.get("schedule_time") or "04:00").split(":"))
        if not last_ok:
            due = now
        else:
            base = dt.datetime.fromtimestamp(last_ok)
            cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= base:
                cand += dt.timedelta(days=1)
            due = cand.timestamp()
    else:
        return None
    if attempt and not attempt.get("ok") and int(attempt.get("fails") or 0) > 0:
        fails = int(attempt["fails"])
        due = max(due, float(attempt.get("ts") or 0) + RETRY[min(fails, len(RETRY)) - 1])
    return due


def human_when(ts: float, now: float | None = None) -> str:
    """"сегодня в 04:00", "завтра в 04:00", "3 октября в 04:00"; "скоро" for the past."""
    now = time.time() if now is None else now
    if ts <= now + 60:
        return "скоро"
    t, n = dt.datetime.fromtimestamp(ts), dt.datetime.fromtimestamp(now)
    days = (t.date() - n.date()).days
    hm = t.strftime("%H:%M")
    if days == 0:
        return f"сегодня в {hm}"
    if days == 1:
        return f"завтра в {hm}"
    return f"{t.day} {MONTHS_GEN[t.month - 1]} в {hm}"


class Scheduler:
    """Every 30 s: submit a sync for each archive that is due (if scheduling is on in the app settings)."""

    def __init__(self, app: Any, first_delay: float = 10.0, period: float = 30.0):
        self.app = app
        self.first_delay = first_delay
        self.period = period
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self) -> "Scheduler":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()

    def due_list(self, now: float | None = None) -> list[tuple[str, float | None]]:
        """(nick, next run) for every archive; used by the scheduler and by the UI / tray."""
        from .state import Archive, archive_dirs
        now = time.time() if now is None else now
        out = []
        for d in archive_dirs(self.app.library):
            arch = Archive(d, self.app.library)
            try:
                if not arch.exists():
                    continue
                last = (arch.get_meta("last_sync") or {}).get("finished")
                if arch.get_meta("guard"):   # stopped by the archive guard: no retries until the user decides
                    out.append((d.name, None))
                    continue
                out.append((d.name, next_run(arch.settings(), last, arch.get_meta("sync_attempt"), now)))
            finally:
                arch.close()
        return out

    def tick(self, now: float | None = None, submit: Callable[..., Any] | None = None) -> list[str]:
        if not self.app.settings().get("autosync", True):
            return []
        now = time.time() if now is None else now
        started = []
        for nick, due in self.due_list(now):
            if due is not None and due <= now and not self.app.jobs.busy(nick):
                (submit or self.app.jobs.submit)(nick, "sync", reason="schedule")
                log.info(f"[расписание] синхронизация @{nick}")
                started.append(nick)
        return started

    def _loop(self) -> None:
        if self.stop_event.wait(self.first_delay):
            return
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must survive a broken archive
                log.exception("[расписание] ошибка проверки")
            if self.stop_event.wait(self.period):
                return
