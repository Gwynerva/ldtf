"""Background jobs of the app: sync (+ build) and rebuild.

Up to `max_parallel` jobs run at once, one per archive. All syncs share the app's NetPool, so the total load on
DTF stays within one budget however many archives are syncing (DTF rate limits are per IP).
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from ..state import Archive
from ..util import log

STAGES = OrderedDict([
    ("profile", "Профиль"), ("posts", "Посты"), ("comments", "Комментарии"),
    ("threads", "Контекст обсуждений"), ("media", "Медиафайлы"), ("build", "Сборка архива"),
])
BUILD_STEPS = {"load-posts": "чтение постов", "load-comments": "чтение комментариев", "load-threads": "чтение веток",
               "build-posts": "база: посты", "build-comments": "база: комментарии", "build-search": "поисковый индекс",
               "build-months": "база: ленты", "export-posts": "Markdown и data: посты",
               "export-months": "Markdown: комментарии", "done": "готово"}


class Job:
    _seq = 0

    def __init__(self, nick: str, kind: str, params: dict):
        Job._seq += 1
        self.id = Job._seq
        self.nick = nick
        self.kind = kind            # sync | render
        self.params = params
        self.state = "queued"       # queued | running | done | cancelled | error | blocked (archive guard)
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.stages: OrderedDict[str, dict] = OrderedDict(
            (k, {"title": t, "status": "pending"}) for k, t in STAGES.items()
            if kind == "sync" or k == "build")
        self.log: deque[str] = deque(maxlen=400)
        self.error: str | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def report(self, stage: str, fields: dict) -> None:
        with self.lock:
            st = self.stages.setdefault(stage, {"title": STAGES.get(stage, stage), "status": "pending"})
            st.update({k: v for k, v in fields.items() if v is not None})
            if "status" in fields and fields["status"] == "running" and "t0" not in st:
                st["t0"] = time.time()
            st["updated"] = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            stages = []
            for key, st in self.stages.items():
                s = {k: v for k, v in st.items() if k not in ("t0",)}
                s["key"] = key
                done, total = st.get("done"), st.get("total")
                if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total:
                    s["pct"] = round(100 * min(done, total) / total, 1)
                    rate = st.get("rate") or 0
                    if st.get("status") == "running" and rate > 0 and total > done:
                        s["eta"] = int((total - done) / rate)
                if key == "build" and st.get("phase"):
                    s["phaseTitle"] = BUILD_STEPS.get(st["phase"], st["phase"])
                stages.append(s)
            return {"id": self.id, "nick": self.nick, "kind": self.kind, "state": self.state, "params": self.params,
                    "created": self.created, "started": self.started, "finished": self.finished,
                    "error": self.error, "stages": stages, "log": list(self.log)[-80:]}


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


PRIORITY_REASONS = ("manual", "create", "cli")


class JobManager:
    def __init__(self, library: Path, on_change: Callable[[str], None] | None = None, net: Any = None,
                 max_parallel: int = 1, on_finish: Callable[[Job], None] | None = None):
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
        self._threads: list[threading.Thread] = []
        self._spawn()

    def _spawn(self) -> None:
        while len(self._threads) < self.max_parallel:
            t = threading.Thread(target=self._worker, name="jobs-idle", daemon=True)
            self._threads.append(t)
            t.start()

    def set_parallel(self, n: int) -> None:
        with self.cond:
            self.max_parallel = max(1, n)
            self._spawn()
            self.cond.notify_all()

    # ------------------------------------------------------------------ API
    def submit(self, nick: str, kind: str = "sync", **params: Any) -> Job:
        with self.cond:
            for j in list(self.queue) + self.running:
                if j.nick == nick and j.state in ("queued", "running") and (j.kind == kind or j.state == "running"):
                    return j
            job = Job(nick, kind, params)
            self.jobs[job.id] = job
            while len(self.jobs) > 50:
                old = next((k for k, j in self.jobs.items() if j.state not in ("queued", "running")), None)
                if old is None:
                    break
                self.jobs.pop(old)
            if params.get("reason") in PRIORITY_REASONS:  # what the user asked for goes before scheduled syncs
                pos = next((i for i, j in enumerate(self.queue) if j.params.get("reason") not in PRIORITY_REASONS),
                           len(self.queue))
                self.queue.insert(pos, job)
            else:
                self.queue.append(job)
            self.cond.notify_all()
            return job

    def cancel(self, job_id: int) -> bool:
        with self.cond:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.state == "queued":
                job.state = "cancelled"
                job.finished = time.time()
                try:
                    self.queue.remove(job)
                except ValueError:
                    pass
                return True
            if job.state == "running":
                job.cancel.set()
                job.log.append("Останавливаю… прогресс сохраняется")
                return True
        return False

    def stop(self, timeout: float = 20.0) -> None:
        """App shutdown: drop the queue, ask running jobs to stop (progress is kept) and wait for them."""
        with self.cond:
            self.stopping = True
            for j in list(self.queue):
                j.state, j.finished = "cancelled", time.time()
            self.queue.clear()
            for j in self.running:
                j.cancel.set()
            self.cond.notify_all()
            deadline = time.monotonic() + timeout
            while self.running and time.monotonic() < deadline:
                self.cond.wait(0.5)

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def for_nick(self, nick: str) -> Job | None:
        """The active/queued job of an archive, else its latest finished one."""
        latest = None
        for j in reversed(list(self.jobs.values())):
            if j.nick != nick:
                continue
            if j.state in ("queued", "running"):
                return j
            latest = latest or j
        return latest

    def busy(self, nick: str) -> bool:
        j = self.for_nick(nick)
        return bool(j and j.state in ("queued", "running"))

    def active(self) -> list[Job]:
        with self.cond:
            return list(self.running) + list(self.queue)

    # ------------------------------------------------------------------ workers
    def _worker(self) -> None:
        me = threading.current_thread()
        while True:
            with self.cond:
                while (not self.queue or self.stopping or len(self.running) >= self.max_parallel
                       or self._threads.index(me) >= self.max_parallel):
                    self.cond.wait(5)
                job = self.queue.popleft()
                self.running.append(job)
            me.name = f"job{job.id}-main"
            try:
                self._run(job)
            except BaseException as e:  # noqa: BLE001 - SystemExit from a stage must not kill the worker
                job.state = "error"
                job.error = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
                job.log.append(traceback.format_exc()[-2000:])
            finally:
                job.finished = time.time()
                me.name = "jobs-idle"
                with self.cond:
                    if job in self.running:
                        self.running.remove(job)
                    self.cond.notify_all()
                for cb in (self.on_change, ):
                    try:
                        cb(job.nick)
                    except Exception:  # noqa: BLE001
                        pass
                if self.on_finish:
                    try:
                        self.on_finish(job)
                    except Exception:  # noqa: BLE001
                        pass

    def _run(self, job: Job) -> None:
        from ..render import render
        from ..sync import Syncer
        job.state = "running"
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
                s = arch.settings()
                net = self.net
                code = Syncer(arch, job.params.get("user") or arch.get_meta("user_ident") or job.nick,
                              workers=net.api_conn if net else int(s["workers"]),
                              media_workers=net.media_conn if net else int(s["media_workers"]),
                              refresh_days=int(s["refresh_days"]), full=bool(job.params.get("full")),
                              no_media=not s["media"], rate=float(s["rate"]),
                              reporter=job.report, cancel=job.cancel,
                              net=net.lease() if net else None, name=prefix.rstrip("-"),
                              accept=bool(job.params.get("accept"))).run()
                if code == 4:   # the archive guard stopped it: nothing to build, no retry until the user decides
                    g = arch.get_meta("guard") or {}
                    job.state = "blocked"
                    job.error = g.get("message") or "синхронизация остановлена защитой архива"
                    job.report("build", {"status": "skipped"})
                    return
                if job.cancel.is_set() or code == 130:
                    job.state = "cancelled"
                    job.report("build", {"status": "skipped"})
                    return
                _note_attempt(arch, ok=code == 0)
                if code == 3:
                    raise RuntimeError("с этим архивом уже работает другой процесс синхронизации")
                if code == 2:
                    job.error = "сеть недоступна или DTF ограничил запросы — прогресс сохранён, повторю позже"
                if not arch.raw_profile().exists():  # nothing downloaded yet: nothing to build
                    job.report("build", {"status": "skipped"})
                    job.state = "error"
                    job.error = job.error or "профиль не загружен"
                    return
            job.report("build", {"status": "running", "phase": "load-posts"})
            render(arch, progress=lambda phase, d, t: job.report(
                "build", {"status": "running", "phase": phase, "done": d, "total": t or None}))
            job.report("build", {"status": "done", "phase": "done"})
            job.state = "error" if job.error else "done"
        finally:
            log.removeHandler(cap)
            if fh:
                log.removeHandler(fh)
                fh.close()
            arch.close()


def _note_attempt(arch: Archive, ok: bool) -> None:
    """Scheduler bookkeeping: when the last sync ran and how many failed in a row (retry backoff)."""
    prev = arch.get_meta("sync_attempt") or {}
    arch.set_meta("sync_attempt", {"ts": int(time.time()), "ok": ok,
                                   "fails": 0 if ok else int(prev.get("fails", 0)) + 1})
    arch.commit()


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
