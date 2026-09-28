"""`dtf-backup app`: LDTF as a desktop app — server in the background, icon in the Windows tray.

Without a tray (not Windows, or the tray failed) it behaves like `serve`: a console app stopped with Ctrl+C.
A second start opens the browser on the running instance (or does nothing with --background).
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .scheduler import human_when
from .util import log
from .web.jobs import job_percent
from .web.server import Runtime, find_running


def run_app(library: Path, port: int = 8765, background: bool = False) -> int:
    running = find_running(library, port)
    if running:
        if not background:
            webbrowser.open(running["url"])
        return 0
    rt = Runtime(library, port, scheduler_delay=120.0 if background else 10.0).start()
    app = rt.app

    def open_ui(path: str = "") -> None:
        webbrowser.open(rt.url + path.lstrip("/"))

    if not background and app.settings().get("open_browser", True):
        threading.Timer(0.6, open_ui).start()

    from . import tray as traymod
    if not traymod.available():
        return _console(rt)
    try:
        ui = TrayUI(rt, open_ui)
        ui.tray.run()          # blocks until "Остановить LDTF" (or Windows logs off)
    except Exception:  # noqa: BLE001 - no tray: keep the app usable from the console
        log.exception("[трей] не удалось показать значок в трее")
        return _console(rt)
    rt.stop()
    return 0


def _console(rt: Runtime) -> int:
    rt.app.on_quit = lambda: threading.Thread(target=rt.stop, daemon=True).start()
    try:
        while not rt.stopped.wait(0.5):
            pass
    except KeyboardInterrupt:
        rt.stop()
    return 0


class TrayUI:
    """Status, menu and notifications of the tray icon on top of the running app."""

    def __init__(self, rt: Runtime, open_ui: Any):
        from . import tray as traymod
        from . import winintegration as win
        self.rt, self.app, self.open_ui, self.win, self.tm = rt, rt.app, open_ui, win, traymod
        self.stopping = False
        self._next: tuple[float, tuple[str, float] | None] = (0.0, None)
        self.tray = traymod.Tray(self.status, self.menu, lambda: open_ui(), on_end_session=lambda: rt.stop(5))
        self.app.on_quit = self.quit
        self.app.finish_listeners.append(self.job_finished)

    # ------------------------------------------------------------------ state
    def _next_sync(self) -> tuple[str, float] | None:
        """Nearest scheduled sync (cached for a minute: it reads every archive's state)."""
        ts, val = self._next
        if time.time() - ts > 60:
            due = [(n, t) for n, t in self.app.scheduler.due_list() if t is not None]
            val = min(due, key=lambda x: x[1]) if due and self.app.settings().get("autosync", True) else None
            self._next = (time.time(), val)
        return val

    def _errors(self) -> list[str]:
        latest: dict[str, Any] = {}
        for j in self.app.jobs.jobs.values():
            if j.kind == "sync" and j.state in ("done", "error"):
                latest[j.nick] = j
        return [n for n, j in latest.items() if j.state == "error"]

    def status(self) -> dict:
        if self.stopping:
            return {"state": "sync", "tip": "LDTF — останавливается, прогресс сохраняется…"}
        running = [j for j in self.app.jobs.active() if j.state == "running"]
        if running:
            parts = [f"@{j.nick} {job_percent(j.snapshot())}%" for j in running]
            return {"state": "sync", "tip": "LDTF — синхронизация: " + ", ".join(parts)}
        guarded = [a["nick"] for a in self.app.accounts() if a.get("guard")]
        if guarded:
            return {"state": "error", "tip": f"LDTF — нужна проверка архива @{guarded[0]}: синхронизация остановлена"}
        errors = self._errors()
        if errors:
            return {"state": "error", "tip": f"LDTF — ошибка синхронизации @{errors[0]}"}
        nxt = self._next_sync()
        tail = f" · следующая {human_when(nxt[1])}" if nxt else ""
        return {"state": "idle", "tip": f"LDTF {__version__} — работает{tail}"}

    def menu(self) -> list:
        tm, win = self.tm, self.win
        st = self.status()["tip"].replace("LDTF — ", "").replace(f"LDTF {__version__} — ", "")
        auto = bool(self.app.settings().get("autosync", True))
        items: list = [tm.MenuItem("Открыть LDTF", self.open_ui, default=True), tm.SEP,
                       tm.MenuItem(st[:1].upper() + st[1:], enabled=False), tm.SEP,
                       tm.MenuItem("Синхронизировать все архивы", self.sync_all),
                       tm.MenuItem("Автосинхронизация по расписанию", self.toggle_autosync, checked=auto)]
        if win.available():
            items.append(tm.MenuItem("Запускать вместе с Windows", self.toggle_autostart,
                                     checked=win.autostart_enabled()))
        items += [tm.MenuItem("Настройки…", lambda: self.open_ui("app")), tm.SEP,
                  tm.MenuItem("Остановить LDTF", self.quit)]
        return items

    # ------------------------------------------------------------------ actions
    def sync_all(self) -> None:
        from .state import archive_dirs
        for d in archive_dirs(self.app.library):
            self.app.jobs.submit(d.name, "sync", reason="manual")
        self.tray.refresh()

    def toggle_autosync(self) -> None:
        self.app.save_settings({"autosync": not self.app.settings().get("autosync", True)}, partial=True)
        self._next = (0.0, None)
        self.tray.refresh()

    def toggle_autostart(self) -> None:
        try:
            self.win.set_autostart(not self.win.autostart_enabled())
        except OSError as e:
            self.tray.notify("LDTF", f"Не удалось изменить автозапуск: {e}")

    def quit(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self.tray.refresh()
        self.rt.stop()
        self.tray.close()

    def job_finished(self, job: Any) -> None:
        self._next = (0.0, None)
        self.tray.refresh()
        if job.kind != "sync" or not self.app.settings().get("notify", True) or self.stopping:
            return
        link = lambda: self.open_ui(f"u/{job.nick}/sync")  # noqa: E731
        if job.state == "done":
            from .guard import site_summary
            from .state import peek_meta
            mins = max(1, round(((job.finished or time.time()) - (job.started or time.time())) / 60))
            site = site_summary(((peek_meta(self.app.library / job.nick, "last_sync") or {}).get("stats") or {}).get("site"))
            text = f"Готово за {mins} мин." + (f" На DTF пропало: {site} — в архиве сохранены." if site else "")
            self.tray.notify(f"Архив @{job.nick} синхронизирован", text, link)
        elif job.state == "blocked":
            self.tray.notify(f"Синхронизация @{job.nick} остановлена", (job.error or "нужна проверка")[:200], link)
        elif job.state == "error":
            self.tray.notify(f"Ошибка синхронизации @{job.nick}", (job.error or "подробности в журнале")[:200], link)
