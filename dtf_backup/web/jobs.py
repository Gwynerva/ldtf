"""Background jobs of the app: sync (+ build), rebuild, and dropping the comments of an archive switched to posts only.

Up to MAX_PARALLEL jobs run at once, never two of one archive. All syncs share the app's NetPool, so the total load on
DTF stays within one budget however many archives are syncing (DTF rate limits are per IP). The shared media store is
cleaned here too: state.gc_media holds every archive's sync lock, so it runs only when no job does, and no job starts
while it runs (a starting sync would otherwise find its archive "locked").
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from ..guard import STOPPED
from ..scope import drop_locked, has_comment_data
from ..state import META_GUARD, META_SYNC_ATTEMPT, Archive, gc_media, gc_pending, state_db
from ..util import human_bytes, log

KINDS = ("sync", "render", "purge")
MAX_PARALLEL = 2   # archives synced at once: a long first download doesn't hold up the others' scheduled syncs
STAGES = OrderedDict([
    ("purge", "Удаление комментариев"), ("profile", "Профиль"), ("posts", "Посты"), ("comments", "Комментарии"),
    ("threads", "Контекст обсуждений"), ("media", "Медиафайлы"), ("build", "Сборка архива"),
])
# job states: (title, icon); the app bar, the sync page and the tray show them as they come from snapshot()
QUEUED, RUNNING, DONE, CANCELLED, ERROR, BLOCKED = "queued", "running", "done", "cancelled", "error", "blocked"
ACTIVE = (QUEUED, RUNNING)
JOB_STATES = {QUEUED: ("В очереди", "schedule"), RUNNING: ("Идёт синхронизация", "sync"), DONE: ("Готово", "check_circle"),
              CANCELLED: ("Остановлено — прогресс сохранён", "cancel"), ERROR: ("Ошибка", "error"),
              BLOCKED: ("Остановлено защитой архива", "gpp_maybe")}   # BLOCKED: the archive guard (guard.py)
RUNNING_TITLES = {"sync": "Идёт синхронизация", "render": "Идёт пересборка страниц", "purge": "Удаление комментариев"}
STAGE_STATES = {"pending": ("ожидает", "radio_button_unchecked"), "running": ("идёт", "progress_activity"),
                "done": ("готово", "check_circle"), "skipped": ("пропущено", "block"), "stopped": ("остановлено", "cancel"),
                "error": ("ошибка", "error")}
EXIT_CODES = {DONE: 0, CANCELLED: 130, BLOCKED: 4}   # a job run for the CLI ends like `sync` would (sync.Exit); else 2
STATE_ICONS = sorted({ic for _, ic in (*JOB_STATES.values(), *STAGE_STATES.values())})   # the page needs them for app.js
BUILD_STEPS = {"load-posts": "чтение постов", "load-comments": "чтение комментариев", "load-threads": "чтение веток",
               "build-posts": "страницы постов", "build-comments": "страницы комментариев", "build-search": "поиск",
               "build-months": "лента по месяцам", "export-posts": "файлы для чтения без LDTF: посты",
               "export-months": "файлы для чтения без LDTF: комментарии", "done": "готово"}
BUILD_ORDER = list(BUILD_STEPS)   # the build reports its steps in this order: its percent spans all of them
PRIORITY_REASONS = ("manual", "create", "cli")   # what the user asked for goes before scheduled syncs


def plan_stages(arch: Archive | None, kind: str) -> list[str]:
    """The stages a job of this kind goes through for this archive, by its settings now: an archive of posts only has
    no comment stages (and a first "purge" while comments are left), without media downloads no media stage."""
    if kind == "render":
        return ["build"]
    if kind == "purge":
        return ["purge", "build"]
    keys = ["profile", "posts", "comments", "threads", "media", "build"]
    if arch is None:
        return keys
    s = arch.settings()
    if s["scope"] == "posts":
        keys = [k for k in keys if k not in ("comments", "threads")]
        if has_comment_data(arch):
            keys.insert(0, "purge")
    if s["media"] == "off":
        keys.remove("media")
    return keys


class Job:
    _seq = 0

    def __init__(self, nick: str, kind: str, params: dict, stages: list[str] | None = None):
        Job._seq += 1
        self.id = Job._seq
        self.nick = nick
        self.kind = kind            # KINDS
        self.params = params
        self.state = QUEUED         # JOB_STATES
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.stages: OrderedDict[str, dict] = OrderedDict()
        self.set_stages(stages or plan_stages(None, kind))
        self.log: deque[str] = deque(maxlen=400)
        self.error: str | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def set_stages(self, keys: list[str]) -> None:
        self.stages = OrderedDict((k, {"title": STAGES.get(k, k), "status": "pending"}) for k in keys)

    def report(self, stage: str, fields: dict) -> None:
        """Progress of a stage; stages the job doesn't show (skipped by the archive's settings) are ignored."""
        with self.lock:
            st = self.stages.get(stage)
            if st is None:
                return
            st.update({k: v for k, v in fields.items() if v is not None})
            if "status" in fields and fields["status"] == "running" and "t0" not in st:
                st["t0"] = time.time()
            st["updated"] = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            stages = []
            live = self.state in ACTIVE
            for key, st in self.stages.items():
                s = {k: v for k, v in st.items() if k not in ("t0",)}
                s["key"] = key
                if not live and s.get("status") == "running":   # the job ended in the middle of this stage
                    s["status"] = ERROR if self.state == ERROR else "stopped"
                    s.pop("eta", None)
                done, total = st.get("done"), st.get("total")
                if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total:
                    s["pct"] = round(100 * min(done, total) / total, 1)
                    rate = st.get("rate") or 0
                    if st.get("status") == "running" and rate > 0 and total > done:
                        s["eta"] = int((total - done) / rate)
                if key == "build" and st.get("phase"):
                    s["phaseTitle"] = BUILD_STEPS.get(st["phase"], st["phase"])
                    if st["phase"] in BUILD_ORDER and s.get("status") == "running":   # one percent for all the steps
                        i = BUILD_ORDER.index(st["phase"])
                        s["pct"] = round(100 * (i + (s.get("pct") or 0) / 100) / (len(BUILD_ORDER) - 1), 1)
                    s.pop("done", None)   # counts of one step ("113 / 113" while reading posts) would mislead
                    s.pop("total", None)
                s["statusTitle"], s["icon"] = STAGE_STATES.get(s.get("status"), (s.get("status"), "radio_button_unchecked"))
                stages.append(s)
            title, ic = JOB_STATES.get(self.state, (self.state, "sync"))
            if self.state == RUNNING:
                title = RUNNING_TITLES.get(self.kind, title)
            snap = {"id": self.id, "nick": self.nick, "kind": self.kind, "state": self.state, "title": title, "icon": ic,
                    "params": self.params, "created": self.created, "started": self.started, "finished": self.finished,
                    "error": self.error, "stages": stages, "log": list(self.log)[-80:],
                    "stoppable": live and self.kind == "sync"}
            snap["percent"] = job_percent(snap)
            return snap


class _ThreadFilter(logging.Filter):
    """Only records logged by one job's threads (its main thread and its sync's worker pools)."""

    def __init__(self, prefix: str):
        super().__init__()
        self.prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.threadName.startswith(self.prefix)


class _Capture(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.log.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class JobManager:
    def __init__(self, library: Path, on_change: Callable[[str], None] | None = None, net: Any = None,
                 max_parallel: int = MAX_PARALLEL, on_finish: Callable[[Job], None] | None = None):
        self.library = library
        self.on_change = on_change or (lambda nick: None)
        self.on_finish = on_finish
        self.net = net                      # NetPool shared by all syncs (None: each sync limits itself)
        self.max_parallel = max(1, max_parallel)
        self.jobs: OrderedDict[int, Job] = OrderedDict()
        self.queue: deque[Job] = deque()
        self.running: list[Job] = []
        self.cond = threading.Condition()
        self.stopping = False
        self.gc_wanted = False              # the shared media store has files to free: as soon as no job runs
        self.gc_running = False             # no job starts meanwhile (gc_media holds every archive's lock)
        self.auto_retry: Callable[[str], bool] | None = None   # will the scheduler retry this archive by itself?
        self._threads: list[threading.Thread] = []
        self._spawn()

    def _spawn(self) -> None:
        while len(self._threads) < self.max_parallel:
            t = threading.Thread(target=self._worker, name="jobs-idle", daemon=True)
            self._threads.append(t)
            t.start()

    def _stages(self, nick: str, kind: str) -> list[str]:
        d = self.library / nick
        try:
            return plan_stages(Archive(d, self.library) if d.is_dir() else None, kind)
        except Exception:  # noqa: BLE001 - a damaged settings file must not stop a job from being queued
            log.exception(f"[задания] этапы задания @{nick}")
            return plan_stages(None, kind)

    # ------------------------------------------------------------------ API
    def submit(self, nick: str, kind: str = "sync", **params: Any) -> Job:
        """Queue a job (the same kind already queued or running for the archive is returned instead). A sync also
        builds the archive, so it replaces a rebuild still waiting in the queue, and a rebuild asked while a sync
        waits is that sync."""
        assert kind in KINDS, kind
        with self.cond:
            for j in list(self.queue) + self.running:
                if j.nick == nick and j.state in ACTIVE and (j.kind == kind or (
                        kind == "render" and j.kind == "sync" and j.state == QUEUED)):
                    return j
            if kind == "sync":
                for j in [j for j in self.queue if j.nick == nick and j.kind == "render"]:
                    self.queue.remove(j)
                    self.jobs.pop(j.id, None)
            job = Job(nick, kind, params, self._stages(nick, kind))
            self.jobs[job.id] = job
            while len(self.jobs) > 50:
                old = next((k for k, j in self.jobs.items() if j.state not in ACTIVE), None)
                if old is None:
                    break
                self.jobs.pop(old)
            if params.get("reason") in PRIORITY_REASONS:
                pos = next((i for i, j in enumerate(self.queue) if j.params.get("reason") not in PRIORITY_REASONS),
                           len(self.queue))
                self.queue.insert(pos, job)
            else:
                self.queue.append(job)
            self.cond.notify_all()
            return job

    def cancel(self, job_id: int) -> bool:
        """Stop a job (progress is kept). A queued job of any kind is dropped; a running rebuild or drop of comments
        can't be stopped halfway (both are short) — and needn't be: the archive's next sync redoes them anyway."""
        with self.cond:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.state == QUEUED:
                job.state = CANCELLED
                job.finished = time.time()
                try:
                    self.queue.remove(job)
                except ValueError:
                    pass
                return True
            if job.state == RUNNING and job.kind == "sync":
                job.cancel.set()
                job.log.append("Останавливаю… прогресс сохраняется")
                return True
        return False

    def stop(self, timeout: float = 20.0) -> None:
        """App shutdown: drop the queue, ask running jobs to stop (progress is kept) and wait for them."""
        with self.cond:
            self.stopping = True
            for j in list(self.queue):
                j.state, j.finished = CANCELLED, time.time()
            self.queue.clear()
            for j in self.running:
                j.cancel.set()
            self.cond.notify_all()
            deadline = time.monotonic() + timeout
            while (self.running or self.gc_running) and time.monotonic() < deadline:
                self.cond.wait(0.5)

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def for_nick(self, nick: str) -> Job | None:
        """The active/queued job of an archive, else its latest finished one."""
        latest = None
        for j in reversed(list(self.jobs.values())):
            if j.nick != nick:
                continue
            if j.state in ACTIVE:
                return j
            latest = latest or j
        return latest

    def busy(self, nick: str) -> bool:
        j = self.for_nick(nick)
        return bool(j and j.state in ACTIVE)

    def purging(self, nick: str) -> bool:
        with self.cond:
            return any(j.nick == nick and j.kind == "purge" for j in list(self.queue) + self.running)

    def active(self) -> list[Job]:
        with self.cond:
            return list(self.running) + list(self.queue)

    # ------------------------------------------------------------------ shared media store
    def want_gc(self) -> None:
        """Free the files no archive needs any more, as soon as no job runs."""
        with self.cond:
            self.gc_wanted = True
            self.cond.notify_all()

    def run_gc(self, me: Job | None = None) -> dict | None:
        """Clean the shared media store now if no other job runs (`me`: the job asking); else as soon as none does.
        Returns gc_media's result, or None when it was left for later."""
        with self.cond:
            if self.gc_running or any(j is not me for j in self.running):
                self.gc_wanted = True
                self.cond.notify_all()
                return None
            self.gc_running, self.gc_wanted = True, False
        return self._gc()

    def _gc(self) -> dict | None:
        try:
            r = gc_media(self.library)
            if r["status"] == "done" and r["files"]:
                log.info(f"[медиа] из общего хранилища удалено ненужных файлов: {r['files']}, {human_bytes(r['bytes'])}")
            return r
        except Exception:  # noqa: BLE001 - the store stays as it was; the marker makes the next start retry
            log.exception("[медиа] очистка общего хранилища")
            return None
        finally:
            with self.cond:
                self.gc_running = False
                self.cond.notify_all()

    # ------------------------------------------------------------------ workers
    def _next(self) -> Job | None:
        """The first queued job whose archive is free (and at most one rebuild at a time: it loads the whole archive)."""
        busy = {j.nick for j in self.running}
        render = any(j.kind != "sync" for j in self.running)
        for j in self.queue:
            if j.nick not in busy and not (j.kind != "sync" and render):
                return j
        return None

    def _worker(self) -> None:
        me = threading.current_thread()
        while True:
            job = None
            with self.cond:
                while True:
                    if not self.stopping and self._threads.index(me) < self.max_parallel and not self.gc_running:
                        if self.gc_wanted and not self.running:
                            self.gc_running, self.gc_wanted = True, False
                            break
                        if len(self.running) < self.max_parallel:
                            job = self._next()
                            if job:
                                self.queue.remove(job)
                                self.running.append(job)
                                break
                    self.cond.wait(5)
            if job is None:
                self._gc()
                continue
            me.name = f"job{job.id}-main"
            try:
                job.set_stages(self._stages(job.nick, job.kind))   # the settings may have changed while it waited
                self._run(job)
            except BaseException as e:  # noqa: BLE001 - SystemExit from a stage must not kill the worker
                job.state = ERROR
                job.error = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
                job.log.append(traceback.format_exc()[-2000:])
                if job.kind == "sync" and state_db(self.library / job.nick).exists():
                    _note_attempt_at(self.library / job.nick, self.library, ok=False)   # the scheduler backs off
            finally:
                job.finished = time.time()
                me.name = "jobs-idle"
                with self.cond:
                    if job in self.running:
                        self.running.remove(job)
                    if not self.queue and not self.running and gc_pending(self.library).exists():
                        self.gc_wanted = True   # a cleanup postponed earlier (a sync was running then)
                    self.cond.notify_all()
                for cb, arg in ((self.on_change, job.nick), (self.on_finish, job)):
                    try:
                        if cb:
                            cb(arg)
                    except Exception:  # noqa: BLE001 - the page cache / the tray must not stop the queue
                        log.exception(f"[задания] обработка завершения задания {job.id}")

    def _run(self, job: Job) -> None:
        from ..render import render
        from ..sync import Exit, Syncer
        job.state = RUNNING
        job.started = time.time()
        arch = Archive(self.library / job.nick, self.library)
        prefix = f"job{job.id}-"
        cap = _Capture(job)
        cap.addFilter(_ThreadFilter(prefix))
        fh = None
        log.addHandler(cap)
        try:
            arch.state_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(arch.log_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.addFilter(_ThreadFilter(prefix))
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
            log.addHandler(fh)
            if job.kind == "sync":
                net = self.net
                syncer = Syncer.from_settings(
                    arch, job.params.get("user") or arch.get_meta("user_ident") or job.nick, arch.settings(),
                    workers=net.api_conn if net else None, media_workers=net.media_conn if net else None,
                    full=bool(job.params.get("full")), reporter=job.report, cancel=job.cancel,
                    net=net.lease() if net else None, name=prefix.rstrip("-"), accept=bool(job.params.get("accept")))
                code = syncer.run()
                if syncer.dropped:   # the archive was switched to posts only: free the comments' files
                    self._free_media(job)
                if code == Exit.GUARD:   # the archive guard stopped it: nothing to build, no retry until the user decides
                    g = arch.get_meta(META_GUARD) or {}
                    job.state = BLOCKED
                    job.error = g.get("message") or STOPPED
                    job.report("build", {"status": "skipped"})
                    return
                if job.cancel.is_set() or code == Exit.STOPPED:
                    job.state = CANCELLED
                    job.report("build", {"status": "skipped"})
                    return
                _note_attempt(arch, ok=code == Exit.OK)
                if code == Exit.LOCKED:
                    raise RuntimeError("с этим архивом уже работает другой процесс синхронизации")
                if code == Exit.NETWORK:
                    retry = bool(self.auto_retry and self.auto_retry(job.nick))
                    job.error = (("нет подключения к интернету" if syncer.fatal is not None and syncer.fatal.offline else
                                  "сеть недоступна или DTF ограничил запросы") + " — прогресс сохранён, "
                                 + ("повторю автоматически" if retry else "запустите синхронизацию снова позже"))
                if not arch.raw_profile().exists():  # nothing downloaded yet: nothing to build
                    job.report("build", {"status": "skipped"})
                    job.state = ERROR
                    job.error = job.error or "профиль не загружен"
                    return
            elif job.kind == "purge":
                job.report("purge", {"status": "running"})
                if arch.settings()["scope"] != "posts":   # switched back while the job waited: keep everything
                    log.info("Архив снова хранит комментарии — удалять нечего.")
                    job.report("purge", {"status": "skipped"})
                    job.report("build", {"status": "skipped"})
                    job.state = DONE
                    return
                if has_comment_data(arch):
                    drop_locked(arch)
                job.report("purge", {"status": "done"})
                self._free_media(job)
            if not arch.raw_profile().exists():   # a rebuild of an archive that has never been downloaded
                job.report("build", {"status": "skipped"})
                job.state = DONE
                return
            job.report("build", {"status": "running", "phase": "load-posts"})
            render(arch, progress=lambda phase, d, t: job.report(
                "build", {"status": "running", "phase": phase, "done": d, "total": t or None}))
            job.report("build", {"status": "done", "phase": "done"})
            job.state = ERROR if job.error else DONE
        finally:
            log.removeHandler(cap)
            if fh:
                log.removeHandler(fh)
                fh.close()
            arch.close()

    def _free_media(self, job: Job) -> None:
        r = self.run_gc(me=job)
        if r is None or r["status"] != "done":
            log.info("Файлы медиа, которые были нужны только комментариям, удалятся из общего хранилища, когда "
                     "закончатся другие синхронизации.")
        else:
            log.info(f"Из общего хранилища удалено файлов: {r['files']}, освобождено {human_bytes(r['bytes'])}.")


def _note_attempt(arch: Archive, ok: bool) -> None:
    """Scheduler bookkeeping: when the last sync ran and how many failed in a row (retry backoff)."""
    prev = arch.get_meta(META_SYNC_ATTEMPT) or {}
    arch.set_meta(META_SYNC_ATTEMPT, {"ts": int(time.time()), "ok": ok,
                                   "fails": 0 if ok else int(prev.get("fails", 0)) + 1})
    arch.commit()


def _note_attempt_at(root: Path, library: Path, ok: bool) -> None:
    arch = Archive(root, library)
    try:
        _note_attempt(arch, ok)
    except Exception:  # noqa: BLE001 - bookkeeping of a job that already failed
        log.exception(f"[задания] @{root.name}: попытка синхронизации не записана")
    finally:
        arch.close()


def job_percent(snap: dict) -> int:
    """Share of the whole job: finished stages + the running one's percent (same as the app bar ring)."""
    stages = snap.get("stages") or []
    done = 0.0
    for s in stages:
        if s.get("status") in ("done", "skipped"):
            done += 1
        elif s.get("status") == "running" and s.get("pct") is not None:
            done += s["pct"] / 100
    return min(100, round(100 * done / (len(stages) or 1)))
