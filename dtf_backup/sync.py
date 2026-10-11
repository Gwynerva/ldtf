"""`sync`: download everything into raw/ + media/, resumable and incremental.

Stages: profile -> posts -> comments -> threads -> media. Each stage can be interrupted at
any moment (Ctrl+C, crash, network fuse); every finished unit of work is committed to the
state DB immediately and the next run continues where it stopped.

Archived materials are never replaced with DTF's placeholders (guard.py): what was deleted, wiped or
hidden on the site keeps its archived version with a `_site` mark, and a deleted/frozen account or a
mass loss stops the sync (exit code 4, meta `guard`) before anything is written.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from enum import IntEnum
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import integrity
from .api import Dtf, comment_url, is_not_found, post_url
from .context import prune_context
from .guard import (ACCEPTABLE, COMMENTS_LIMIT, GuardTrip, account_problem, comment_stub, degraded, keep,
                    merge_items, post_loss, post_stub, post_wiped, posts_limit, site_summary,
                    state_title)
from .guard import title as guard_title
from .history import apply_events, content_sig, ensure as ensure_history, import_legacy, record_edit, record_event
from .integrity import Damaged, DbDamaged, read_raw, safe_unpack
from .http import AdaptiveLimiter, FatalNetworkError, HttpClient
from .media import MEDIA_MODES, Downloader, avatar_key, collect_media, effective_media, media_key, owner_sql, raw_key
from .scope import drop_comments, has_comment_data
from .settings import CHOICES, DEFAULTS, clamp
from .state import META_GUARD, META_LAST_SYNC, Archive, pack
from .util import (COMMENTS, DAY, HOUR, MSK, POSTS, count_label, gz_json, human_bytes, log, now_ts, num, short, ts_human,
                   ts_month, ts_year, write_durable, write_json_gz)

STAGES = ("posts", "comments", "threads", "media")
COMMENT_STAGES = ("comments", "threads")   # not run for an archive of posts only (scope "posts")
SCOPES = CHOICES["scope"]
FAR_ID = Dtf.FAR_ID
POST_SKIP_KEYS = ("author", "subsite")
# network of a standalone sync (CLI --standalone); inside the app every sync uses the app's shared budget (netpool.py).
# ~9 rps never triggered 429 in tests, light endpoints at ~18 rps did: start at 10, adapt up to 16.
WORKERS, RATE, MEDIA_WORKERS = 4, 10.0, 8
NET_LIMITS = {"workers": (1, 12), "rate": (1.0, 30.0), "media_workers": (1, 16)}


class Exit(IntEnum):
    """Result of a sync (also the exit code of `sync`)."""
    OK = 0
    NETWORK = 2    # no network or DTF limited us: progress is kept, try later
    LOCKED = 3     # another process syncs this archive
    GUARD = 4      # stopped by the archive guard (meta "guard"): waits for the user
    INTEGRITY = 5  # the archive's database is damaged and could not be restored (integrity.py): nothing was written
    STOPPED = 130  # cancelled / Ctrl+C: progress is kept


class Cancelled(Exception):
    """Raised inside a sync when the app asked it to stop (progress is kept)."""


class Syncer:
    def __init__(self, arch: Archive, user: str | None, workers: int = WORKERS,
                 media_workers: int = MEDIA_WORKERS, refresh_days: int = DEFAULTS["refresh_days"],
                 full: bool = False, only: Iterable[str] | None = None, media: str = DEFAULTS["media"],
                 no_media: bool = False, rate: float = RATE, scope: str = DEFAULTS["scope"],
                 reporter: Callable[[str, dict], None] | None = None, cancel: threading.Event | None = None,
                 net: tuple[Any, Any] | None = None, name: str = "", accept: bool = False,
                 verify: str = "quick", check_only: bool = False):
        """`media`: all | posts | off (media.MEDIA_MODES); `no_media` = "off".
        `scope`: all | posts — an archive of posts only never touches comments (no trees, feed, threads, their media).
        `net`: (api, media) limiters shared with other syncs (the app's NetPool leases); else own ones.
        `name`: thread name prefix, lets the app route this sync's log lines to its own job.
        `accept`: the user reviewed the mass loss that stopped the previous sync and wants to go on.
        `verify`: the integrity check that runs first (integrity.py): quick | full | off. `check_only`: «Проверить
        целостность» — after the check the sync goes on only if something has to be downloaded again."""
        self.arch = arch
        self.reporter = reporter
        self.cancel = cancel
        self.user = user
        self.workers = clamp(NET_LIMITS, "workers", workers)
        self.media_workers = clamp(NET_LIMITS, "media_workers", media_workers)
        rate = clamp(NET_LIMITS, "rate", rate)
        self.refresh_days = refresh_days
        self.full = full
        self.scope = scope if scope in SCOPES else "all"
        self.only = set(only) if only else set(STAGES)
        if self.scope == "posts":
            asked = self.only & set(COMMENT_STAGES)
            if only and asked:
                raise SystemExit(f"Архив хранит только посты — стадии {', '.join(sorted(asked))} для него не выполняются. "
                                 f"Комментарии включаются в настройках архива («Что сохранять»).")
            self.only -= set(COMMENT_STAGES)
        self.media = effective_media("off" if no_media or media not in MEDIA_MODES else media, self.scope)
        if self.media == "off":
            self.only.discard("media")
        self.name = name
        if net is not None:
            self.api_limiter, self.media_limiter = net
        else:
            self.api_limiter = AdaptiveLimiter("api", self.workers, rate=rate, max_rate=max(rate, 16.0))
            self.media_limiter = AdaptiveLimiter("media", self.media_workers)
        self.client = HttpClient({"api.dtf.ru": self.api_limiter, "*": self.media_limiter})
        self.api = Dtf(self.client)
        self.stats: dict[str, Any] = {}
        self.run_ts = now_ts()
        self.accept = accept
        self.accepted: set[str] = set()     # guard kinds confirmed for this run
        self.site: Counter = Counter()      # what disappeared from DTF this run (kept in the archive)
        self.site_items: list[dict] = []    # the first of them, for the report
        self.ident_override: str | None = None
        self.fatal: FatalNetworkError | None = None   # why the network gave up (code 2)
        self.dropped: dict | None = None              # comments dropped at the start (scope "posts"): the store has
                                                      # files to free (state.gc_media)
        self._posts_lost: list[int] = []
        self._posts_limit = 3
        self._posts_archived = 0
        self._comments_lost = 0
        self._comment_samples: list[dict] = []
        self.verify = verify if verify in ("quick", "full", "off") else "quick"   # None: quick
        self.check_only = check_only
        self.check: integrity.Check | None = None
        self._repairs: dict[str, dict] = {}   # kind -> {id: repairs row}: items downloaded again for a repair
        self.network_ran = False

    @classmethod
    def from_settings(cls, arch: Archive, user: str | None, s: dict, **overrides: Any) -> "Syncer":
        """A sync configured by the archive's settings; `overrides` (CLI flags, the app's shared budget, job params)
        win unless None."""
        params: dict[str, Any] = {"refresh_days": int(s["refresh_days"]), "media": s["media"], "scope": s["scope"]}
        params.update({k: v for k, v in overrides.items() if v is not None})
        return cls(arch, user, **params)

    def _write_tree(self, path: Any, items: list[dict], uid: Any, head: dict, threads: set | None = None,
                    prune: list[int] | None = None) -> dict:
        """Merge a fresh comment tree with the archived one (what DTF lost keeps its archived text, guard.merge_items)
        and write it. `threads`: compare only these threads (a branch answer); `prune`: keep only the context of these
        comments (discussion threads). Returns {merged, st (merge stats — with `prune` also _kept/_missing), events
        (history events: the main thread records them, history.apply_events), sig (the file written: (sha256, size)),
        obj}. With events the file is not written here: `data` holds its bytes and the main thread writes it once the
        replaced versions are committed (_save_tree) — a crash in between must not lose them.
        A damaged archived file raises integrity.Damaged: it is never overwritten blindly (integrity.py repairs it)."""
        old = read_raw(path, {}).get("items")
        if old and threads is not None:
            roots = {c.get("id") for c in items if not c.get("replyTo")}
            by = {c.get("id"): c for c in old}

            def root(c: dict) -> Any:
                seen = set()
                while c.get("replyTo") and c["replyTo"] in by and c["id"] not in seen:
                    seen.add(c["id"])
                    c = by[c["replyTo"]]
                return c.get("id")
            # copies restored from the pages have no threadId: their root comment tells the thread
            old = [c for c in old if c.get("threadId") in threads or (not c.get("threadId") and root(c) in roots)]
        events: list = []
        merged, st = merge_items(old, items, now_ts(), uid, events)
        out = merged
        if prune is not None:
            out, missing = prune_context(merged, prune)
            st = dict(st, _kept=len(out), _missing=missing)
            head = dict(head, missing=missing)
            kept = {c.get("id") for c in out}
            events = [e for e in events if e[1].get("id") in kept]
        obj = {**head, "fetchedAt": now_ts(), "items": out}
        res: dict[str, Any] = {"merged": merged, "st": st, "events": events, "path": path,
                               "obj": {"fetchedAt": obj["fetchedAt"]}}   # for the manifest (integrity.content_at)
        if events:
            res["data"] = gz_json(obj)
            return res
        self.check_cancel()   # a request of a stopped sync that came back late: its lock may already be someone else's
        res["sig"] = write_durable(path, gz_json(obj))
        return res

    def _save_tree(self, res: dict, at: int, entry_id: int | None) -> None:
        """Main thread: the history events of a merged tree first (committed), then its file; the file in the
        manifest."""
        db = self.arch.db
        if res.get("events"):
            apply_events(db, res["events"], at, entry_id)
            db.commit()
        if "data" in res:
            res["sig"] = write_durable(res["path"], res.pop("data"))
        integrity.note(db, self.arch, res["path"], res["sig"], res["obj"])

    def report(self, stage: str, **fields: Any) -> None:
        """Structured progress for the app (the console keeps using the log)."""
        if self.reporter:
            try:
                self.reporter(stage, fields)
            except Exception:  # noqa: BLE001 - UI problems must never break a sync
                pass

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------------ runner
    def run(self) -> int:
        lock = self.arch.lock_path
        self.arch.state_dir.mkdir(parents=True, exist_ok=True)
        other = acquire_lock(lock)
        if other:
            log.error(f"С этим архивом уже работает другой sync (PID {other}). Дождитесь его окончания.")
            return Exit.LOCKED
        try:
            return self._run()
        finally:
            release_lock(lock)

    def _run(self) -> int:
        db = self.arch.db
        g = self.arch.get_meta(META_GUARD)
        if g:
            if not (self.accept and g.get("kind") in ACCEPTABLE):
                log.error(f"Синхронизация остановлена защитой архива: {g.get('message')} "
                          f"Решение — на вкладке «Синхронизация» этого архива в LDTF.")
                return Exit.GUARD
            self.accepted.add(g["kind"])
            self.arch.set_meta(META_GUARD, None)
            db.commit()
            log.warning(f"Подтверждено: {guard_title(g['kind'])} — пропавшее будет отмечено, "
                        f"в архиве остаются сохранённые версии.")
        ensure_history(db)
        import_legacy(self.arch)
        db.commit()
        if self.scope == "posts" and has_comment_data(self.arch):   # switched to posts only: drop what is left
            self.report("purge", status="running")
            self.dropped = drop_comments(self.arch)
            self.report("purge", status="done")
        prefix = f"{self.name}-" if self.name else ""
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=prefix + "api")
        mpool = ThreadPoolExecutor(max_workers=self.media_workers, thread_name_prefix=prefix + "media")
        code = Exit.OK
        try:
            if self.verify != "off":
                self.stage_integrity()
            if self.check_only and not self.db_has_repairs():
                for st in ("profile", *STAGES):
                    self.report(st, status="skipped")
                integrity.finish(self.arch, (), checked=self.check is not None)
                log.info("Проверка целостности завершена: скачивать с DTF ничего не нужно.")
                return code
            self.network_ran = True
            for st in STAGES:
                if st not in self.only:
                    self.report(st, status="skipped")
            self.stage_profile()
            if "posts" in self.only:
                self.stage_posts(pool)
            if "comments" in self.only:
                self.stage_comments(pool)
            if "threads" in self.only:
                self.stage_threads(pool)
            if "media" in self.only:
                self.stage_media(mpool)
            self.stats["site"] = dict(self.site)
            if self.site_items:
                self.stats["site_items"] = self.site_items[:30]
            if self.check is not None:
                self.stats["integrity"] = self.check.summary()
            self.arch.set_meta(META_LAST_SYNC, {"ts": self.run_ts, "finished": now_ts(),
                                             "stages": sorted(self.only), "stats": self.stats})
            integrity.finish(self.arch, self.only, checked=self.check is not None)
            if self.site:
                log.warning(f"Изменения на DTF: пропало {site_summary(self.site)} — в архиве сохранены прежние версии.")
            log.info("Синхронизация завершена.")
        except DbDamaged as e:
            code = Exit.INTEGRITY
            log.error(f"База архива повреждена ({e}) и не восстановлена — синхронизация не начиналась, архив не изменён.")
        except GuardTrip as g:
            code = Exit.GUARD
            self.arch.set_meta(META_GUARD, g.as_meta())
            log.error(f"Синхронизация остановлена: {g.message}")
        except FatalNetworkError as e:
            code = Exit.NETWORK
            self.fatal = e
            log.error(f"Сеть недоступна или DTF ограничил запросы: {e}. "
                      f"Прогресс сохранён — синхронизация продолжится с этого места.")
        except (KeyboardInterrupt, Cancelled):
            code = Exit.STOPPED
            log.warning("Остановлено. Прогресс сохранён — запустите синхронизацию снова, чтобы продолжить.")
        finally:
            # stop this sync's waiting workers (a shared limiter keeps serving other syncs)
            for lim in (self.api_limiter, self.media_limiter):
                lim.stopped = True
                with lim.cond:
                    lim.cond.notify_all()
            pool.shutdown(wait=False, cancel_futures=True)
            mpool.shutdown(wait=False, cancel_futures=True)
            _drain([pool, mpool], 15.0)  # requests still on the wire finish before the lock is released
            self.arch.commit()   # (a restored database is a new connection: not `db`)
        return code

    # ------------------------------------------------------------------ integrity
    def stage_integrity(self) -> None:
        """The archive against what LDTF wrote (integrity.py): repairs what local copies can, queues downloads for
        the rest (the stages below pick them up). A failure of the check itself never stops the sync: damaged files
        are not overwritten anyway (read_raw)."""
        self.report("integrity", status="running")

        def check() -> integrity.Check:
            return integrity.Check(self.arch, mode="full" if self.verify == "full" else "quick",
                                   media=self.media != "off" or self.verify == "full",
                                   report=lambda **f: self.report("integrity", status="running", **f),
                                   cancel=self.check_cancel).run()
        try:
            try:
                self.check = check()
            except DbDamaged as e:   # a broken database: restored from its daily copy, then checked again
                log.error(f"[целостность] база {'архива' if e.schema == 'main' else 'медиа'} повреждена: {e}")
                self.report("integrity", status="running", phase="restore")
                (integrity.recover_db if e.schema == "main" else integrity.recover_store)(self.arch)
                self.check = check()
        except (DbDamaged, Cancelled, KeyboardInterrupt):
            raise
        except Exception as e:  # noqa: BLE001
            log.exception(f"[целостность] проверка не удалась: {e}")
            self.report("integrity", status="error", error=str(e)[:200])
            return
        self.report("integrity", status="done", **self.check.summary())
        self._repairs = {k: {i: dict(r) for i, r in integrity.pending(self.arch.db, k).items()}
                         for k in ("post", "tree", "thread", "comment")}

    def db_has_repairs(self) -> bool:
        return self.arch.db.execute("SELECT 1 FROM repairs LIMIT 1").fetchone() is not None

    def run_parallel(self, pool: ThreadPoolExecutor, tasks: Iterable[Any], fn: Callable[[Any], Any],
                     on_done: Callable[[Any, Any, Exception | None], Iterable[Any] | None],
                     label: str, total: int | None = None, progress: Callable[[], dict] | None = None,
                     width: int | None = None, stage: str | None = None) -> int:
        """Run fn(task) in the pool; on_done(task, result, exc) runs in the main thread and may
        return follow-up tasks. Commits every couple of seconds."""
        it: Iterator[Any] = iter(tasks)
        front: deque[Any] = deque()
        pending: dict[Future, Any] = {}
        max_pending = (width or self.workers) * 3
        done_n = 0
        t0 = last_log = last_commit = last_report = time.monotonic()

        def emit(final: bool = False) -> None:
            if stage:
                el = max(time.monotonic() - t0, 1e-6)
                fields: dict[str, Any] = {"status": "done" if final else "running", "done": done_n, "total": total,
                                          "rate": round(done_n / el, 2)}
                if progress:
                    fields.update(progress())
                self.report(stage, **fields)

        def next_task() -> Any:
            if front:
                return front.popleft()
            return next(it)

        def fill() -> None:
            while len(pending) < max_pending:
                try:
                    t = next_task()
                except StopIteration:
                    return
                pending[pool.submit(fn, t)] = t

        fill()
        emit()
        while pending:
            self.check_cancel()
            done, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
            for f in done:
                t = pending.pop(f)
                exc: Exception | None = None
                res = None
                try:
                    res = f.result()
                except FatalNetworkError:
                    self.arch.commit()
                    raise
                except Exception as e:  # noqa: BLE001 - recorded per item, retried next run
                    exc = e
                follow = on_done(t, res, exc)
                if follow:
                    front.extend(follow)
                done_n += 1
            now = time.monotonic()
            if now - last_commit > 2:
                self.arch.commit()
                last_commit = now
            if now - last_report > 0.5:
                emit()
                last_report = now
            if now - last_log > 15:
                rate = done_n / max(now - t0, 1e-6)
                tot = f"/{total}" if total else ""
                extra = (", " + ", ".join(f"{k} {v}" for k, v in progress().items())) if progress else ""
                log.info(f"[{label}] {done_n}{tot} ({rate:.1f}/с){extra}")
                last_log = now
            fill()
        self.arch.commit()
        emit(final=True)
        return done_n

    # ------------------------------------------------------------------ profile
    def stage_profile(self) -> None:
        self.report("profile", status="running")
        a = self.arch
        saved_ident = a.get_meta("user_ident")
        ident = self.user or saved_ident
        if not ident:
            raise SystemExit("Не указан пользователь: добавьте --user <ник|id|ссылка>")
        known = a.get_meta("user_id")
        explicit = bool(self.user) and str(self.user) != str(saved_ident)
        prof = self._fetch_profile(str(ident), known, explicit)
        uid = prof["id"]
        if known is not None and known != uid:
            raise SystemExit(f"Папка уже содержит архив пользователя id={known}, а запрошен id={uid}. "
                             f"Укажите другую папку --out.")
        problem = account_problem(prof)
        if problem:   # nothing is written: the archive keeps the profile, posts and comments it has
            name = a.get_meta("user_name") or ident
            what = "удалён" if problem == "deleted" else "заморожен"
            raise GuardTrip(f"account-{problem}",
                            f"DTF сообщает, что аккаунт «{name}» (id {uid}) {what}. Архив не изменён: всё сохранённое "
                            f"в нём остаётся таким, каким было при последней синхронизации.",
                            {"id": uid, "name": name, "dtfName": prof.get("name"),
                             "posts": a.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                             "comments": a.db.execute("SELECT COUNT(*) FROM my_comments").fetchone()[0]})
        integrity.note(a.db, a, a.raw_profile(), write_json_gz(a.raw_profile(), prof), prof)
        a.set_meta("user_ident", self.ident_override or str(ident))
        a.set_meta("user_id", uid)
        a.set_meta("user_uri", prof.get("uri"))
        a.set_meta("user_name", prof.get("name"))
        a.set_meta("user_created", prof.get("created"))
        a.queue_media(collect_media({"avatar": prof.get("avatar"), "cover": prof.get("cover")}), "profile")
        small = avatar_key(prof.get("avatar"))
        if small:
            a.queue_media([(small, None, "jpg")], f"avatar:{uid}")
        a.commit()
        handle = prof.get("nickname") or (prof.get("uri") or "").strip("/")
        log.info(f"Профиль: {prof.get('name')} ({'@' + handle + ', ' if handle else ''}id {uid})")
        self.report("profile", status="done", name=prof.get("name"))
        try:
            self._save_assets(self.api.assets())
        except Exception as e:  # noqa: BLE001 - catalogs are nice to have
            log.warning(f"Не удалось обновить каталог реакций: {e}")

    def _fetch_profile(self, ident: str, known: int | None, explicit: bool) -> dict:
        """Profile by the saved nickname; if it no longer leads to this account (renamed, or the nickname went to
        someone else), by the stable id. No account under that id either: the guard stops the sync."""
        prof = None
        try:
            prof = self.api.subsite(ident)
        except Exception as e:
            if not is_not_found(e) or known is None:
                raise
        if prof is not None and (known is None or explicit or prof.get("id") == known):
            return prof
        try:
            prof = self.api.subsite(str(known))
        except Exception as e:
            if is_not_found(e):
                name = self.arch.get_meta("user_name") or ident
                raise GuardTrip("account-missing",
                                f"Аккаунт «{name}» (id {known}) не найден на DTF — возможно, он удалён. Архив не изменён.",
                                {"id": known, "name": name}) from None
            raise
        log.warning(f"Ник {ident} больше не ведёт к этому аккаунту — профиль найден по id {known} "
                    f"(сейчас @{prof.get('nickname') or prof.get('uri') or known})")
        self.ident_override = str(known)
        return prof

    def _save_assets(self, fresh: dict) -> None:
        """Reaction/badge catalogs, accumulated over time: entries that disappear from the site are
        kept (marked retired) so old reactions still render."""
        a = self.arch
        try:
            old = read_raw(a.raw_assets(), {})
        except Damaged:   # the catalog is built again from DTF's; the damaged file is kept aside
            integrity.set_aside(a, a.raw_assets())
            old = {}
        out: dict[str, Any] = {"fetchedAt": now_ts()}
        n_new = 0
        for kind in ("reactions", "badges"):
            merged: dict[str, dict] = {str(x.get("id")): {**x, "retired": True} for x in old.get(kind, [])}
            for x in fresh.get(kind) or []:
                merged[str(x.get("id"))] = dict(x)
            out[kind] = list(merged.values())
            for x in out[kind]:
                refs = [(k, None, None) for k in (media_key(x.get("staticUuid")), raw_key(x.get("animatedUuid"))) if k]
                n_new += a.queue_media(refs, f"{kind[:-1]}:{x.get('id')}")
        for k, v in fresh.items():
            if k not in out:
                out[k] = v
        integrity.note(a.db, a, a.raw_assets(), write_json_gz(a.raw_assets(), out), out)
        a.commit()
        log.info(f"Каталог реакций: {len(out['reactions'])}, значков: {len(out['badges'])} (новых картинок в очереди: {n_new})")

    # ------------------------------------------------------------------ posts
    def stage_posts(self, pool: ThreadPoolExecutor) -> None:
        a, db = self.arch, self.arch.db
        uid = a.get_meta("user_id")
        log.info("[посты] получаю список постов…")
        self.report("posts", status="running", phase="list")
        listed: list[dict] = []
        cursor = None
        while True:
            self.check_cancel()
            page, cursor = self.api.timeline_page(uid, cursor)
            listed.extend(page)
            if not page or not cursor:
                break
        by_id = {p["id"]: p for p in listed}
        now = now_ts()
        stubs = self._guard_posts(listed, by_id, now)   # may stop the sync before anything below is written
        for p in listed:
            if p["id"] in stubs:   # a placeholder: keep the archived counters and content, only note it is listed
                db.execute("UPDATE posts SET listed_at=? WHERE id=?", (now, p["id"]))
                continue
            # the listing brings every post's live counters, reactions and donations: a post itself is downloaded
            # again only when it is edited, so the pages take these (viewdb.Dataset)
            db.execute(
                "INSERT INTO posts(id,date,date_modified,comments_count,is_repost,listed_at,stats,stats_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET date=excluded.date, date_modified=excluded.date_modified, "
                "comments_count=excluded.comments_count, is_repost=excluded.is_repost, listed_at=excluded.listed_at, "
                "stats=excluded.stats, stats_at=excluded.stats_at",
                (p["id"], p.get("date"), p.get("dateModified"), (p.get("counters") or {}).get("comments", 0),
                 1 if p.get("repostId") else 0, now, json.dumps(live_stats(p), ensure_ascii=False), now))
        a.set_meta("posts_listed_at", now)
        a.commit()

        recent = now - self.refresh_days * DAY
        tasks: list[tuple[str, int]] = []
        for row in db.execute("SELECT * FROM posts WHERE listed_at=?", (now,)):
            pid = row["id"]
            if pid in stubs:
                continue
            if (self.full or row["content_fetched_at"] is None or row["content_status"] != "ok"
                    or row["content_modified"] != row["date_modified"]):
                tasks.append(("content", pid))
            if self.scope != "posts" and (
                    self.full or row["tree_fetched_at"] is None or row["tree_status"] != "ok"
                    or row["tree_count"] != row["comments_count"] or (row["date"] or 0) >= recent):
                tasks.append(("tree", pid))
        # repairs (integrity.py): damaged posts and trees outside the listing (hidden from the profile) too
        queued = set(tasks)
        rep = self._repairs.get("post", {})
        for pid in list(rep):
            if pid in stubs:   # a placeholder on DTF now: the version the archive had is gone with it
                integrity.settle(a, "post", pid, None, pid)
            elif ("content", pid) not in queued:
                tasks.append(("content", pid))
        if self.scope != "posts":
            for pid in self._repairs.get("tree", {}):
                if ("tree", pid) not in queued and pid not in stubs:
                    tasks.append(("tree", pid))
        trees = sum(1 for t in tasks if t[0] == "tree")
        log.info(f"[посты] в профиле {len(listed)} постов; к загрузке: "
                 f"{sum(1 for t in tasks if t[0] == 'content')} постов"
                 + (f", {trees} деревьев комментариев" if self.scope != "posts" else ""))

        def work(task: tuple[str, int]) -> dict:
            kind, pid = task
            if kind == "content":
                path = a.raw_post(pid)
                old = read_raw(path, None)   # damaged: the task fails, the file stays for integrity.py
                try:
                    data = self.api.content(pid)
                    source = "content"
                except Exception as e:
                    if not is_not_found(e):
                        raise
                    if old is not None and old.get("_source", "content") == "content":
                        return {"keep": "unavailable"}   # never replace a full post with its timeline copy
                    if pid not in by_id:   # not even in the listing (a repair of a hidden post)
                        return {"keep": "gone"}
                    data = dict(by_id[pid])
                    data["_source"] = "timeline"
                    source = "timeline"
                loss = post_loss(old, data)
                if loss:
                    return {"keep": loss}
                # written by done() in the main thread, after the replaced version went to the history
                return {"data": data, "old": old, "dateModified": data.get("dateModified"), "source": source,
                        "media": collect_media(data, POST_SKIP_KEYS)}
            items = self.api.post_comments(pid)
            counter = ((by_id.get(pid) or {}).get("counters") or {}).get("comments", len(items))
            res = self._write_tree(a.raw_post_tree(pid), items, uid, {"postId": pid, "counter": counter})
            media = [(c["id"], collect_media(c.get("media") or [])) for c in items if c.get("media")]
            return {"counter": counter, "n": len(items), "media": media, "tree": res}

        n_media = 0

        def done(task: tuple[str, int], res: dict | None, exc: Exception | None) -> None:
            nonlocal n_media
            kind, pid = task
            ts = now_ts()
            if kind == "content":
                if exc:
                    db.execute("UPDATE posts SET content_status=CASE WHEN content_status='damaged' THEN 'damaged' "
                               "ELSE 'error' END, content_error=? WHERE id=?", (str(exc)[:500], pid))
                    integrity.attempt_failed(db, "post", pid)
                    log.warning(f"[посты] {pid}: ошибка загрузки поста: {exc}")
                    return
                listed_mod = (by_id.get(pid) or {}).get("dateModified")
                if res.get("keep"):
                    db.execute("UPDATE posts SET content_modified=? WHERE id=?", (listed_mod, pid))
                    if pid in rep:
                        db.execute("UPDATE posts SET content_status='ok' WHERE id=? AND content_status='damaged'",
                                   (pid,))
                        integrity.settle(a, "post", pid, None, pid)
                    self._post_lost(pid, res["keep"], ts)
                    return
                old, data = res["old"], res["data"]
                if old is not None and old.get("_source", "content") == "content":   # a timeline copy is no version
                    changed = record_edit(db, "post", old, data, ts, pid)
                    if old.get("_site"):
                        changed = record_event(db, "post", pid, "restored", None, ts, pid) or changed
                    if changed:
                        db.commit()   # the replaced version is safe before the file is overwritten
                path = a.raw_post(pid)
                integrity.note(db, a, path, write_json_gz(path, data), data)
                db.execute("UPDATE posts SET content_status='ok', content_error=NULL, content_fetched_at=?, "
                           "content_modified=?, site_state=NULL, site_state_at=NULL, content_sig=? WHERE id=?",
                           (ts, listed_mod or data.get("dateModified"), content_sig("post", data), pid))
                if pid in rep:
                    integrity.settle(a, "post", pid, data, pid)
                n_media += a.queue_media(res["media"], f"post:{pid}")
            else:
                if exc:
                    db.execute("UPDATE posts SET tree_status=CASE WHEN tree_status='damaged' THEN 'damaged' "
                               "ELSE 'error' END, tree_error=? WHERE id=?", (str(exc)[:500], pid))
                    integrity.attempt_failed(db, "tree", pid)
                    log.warning(f"[посты] {pid}: ошибка загрузки комментариев: {exc}")
                    return
                if res["counter"] and res["n"] < 0.9 * res["counter"]:
                    log.warning(f"[посты] {pid}: получено {res['n']} комментариев из {res['counter']} по счётчику")
                self._save_tree(res["tree"], ts, pid)
                db.execute("UPDATE posts SET tree_status='ok', tree_error=NULL, tree_fetched_at=?, tree_count=? WHERE id=?",
                           (ts, res["counter"], pid))
                integrity.settle_tree(a, "tree", pid, res["tree"]["events"], pid)
                for cid, refs in res["media"]:
                    n_media += a.queue_media(refs, f"pc:{cid}")
                self._context_kept(res["tree"]["st"])

        self.run_parallel(pool, tasks, work, done, "посты", total=len(tasks), stage="posts",
                          progress=lambda: {"listed": len(listed)})
        self.stats["posts"] = {"listed": len(listed), "fetched": len(tasks)}
        log.info(f"[посты] готово; новых медиа в очереди: {n_media}")

    # ------------------------------------------------------------------ guard: posts
    def _guard_posts(self, listed: list[dict], by_id: dict[int, dict], now: int) -> set[int]:
        """Compare the fresh listing with the archive before it is written. Posts that vanished from the profile or
        turned into placeholders count as lost; too many at once stop the sync (GuardTrip). A few are checked one by
        one and marked, keeping their archived versions. Returns ids of listed placeholders (left untouched)."""
        a, db = self.arch, self.arch.db
        prev = a.get_meta("posts_listed_at")
        rows = {r["id"]: dict(r) for r in db.execute("SELECT id, listed_at, content_status, site_state FROM posts")}
        healthy = [pid for pid, r in rows.items() if r["content_status"] == "ok" and not r["site_state"]]
        # only posts seen in the previous listing: ones already hidden from the profile are not news
        vanished = [pid for pid in healthy if prev and rows[pid]["listed_at"] == prev and pid not in by_id]
        stubbed = [p["id"] for p in listed if p["id"] in rows and rows[p["id"]]["content_status"] == "ok"
                   and not rows[p["id"]]["site_state"] and post_stub(p)]
        still = {p["id"] for p in listed if p["id"] in rows and rows[p["id"]]["site_state"] and post_stub(p)}
        for p in listed:   # back to normal on the site (unfrozen, restored): fetch the post again
            r = rows.get(p["id"])
            if r and r["site_state"] in ("removed", "gone") and p["id"] not in still and not post_stub(p):
                db.execute("UPDATE posts SET site_state=NULL, site_state_at=NULL, content_modified=NULL WHERE id=?",
                           (p["id"],))
        self._posts_archived = len(healthy)
        self._posts_limit = posts_limit(len(healthy))
        self._posts_lost = vanished + stubbed
        self._check_posts()
        for pid in vanished:
            self.check_cancel()
            old = load_raw_post(a, pid)
            try:
                data = self.api.content(pid)
                state = "removed" if post_stub(data) else ("wiped" if post_wiped(old, data) else None)
            except FatalNetworkError:
                raise
            except Exception as e:  # noqa: BLE001
                if not is_not_found(e):
                    log.warning(f"[посты] {pid}: пропал из профиля, проверить не удалось: {e}")
                    continue
                state = "gone"
            if state:
                self._mark_post(pid, state, now)
            else:
                log.info(f"[посты] {pid}: скрыт из профиля, но доступен по ссылке")
        for pid in stubbed:
            self._mark_post(pid, "removed", now)
        a.commit()
        return set(stubbed) | still

    def _check_posts(self) -> None:
        lost = self._posts_lost
        if len(lost) < self._posts_limit or "posts-mass" in self.accepted:
            return
        samples = []
        for pid in lost[:10]:
            p = load_raw_post(self.arch, pid) or {}
            samples.append({"id": pid, "title": short(p.get("title"), 120) or "Без заголовка",
                            "url": p.get("url") or post_url(pid)})
        raise GuardTrip(
            "posts-mass",
            f"С прошлой синхронизации на DTF пропали или удалены {count_label(len(lost), *POSTS)} "
            f"из {self._posts_archived} (порог — {self._posts_limit}). Архив не изменён.",
            {"lost": len(lost), "archived": self._posts_archived, "limit": self._posts_limit, "samples": samples})

    def _post_lost(self, pid: int, state: str, ts: int) -> None:
        """A post whose fresh version is a placeholder, a wipe or unavailable: mark it; many stop the sync."""
        cur = self.arch.db.execute("SELECT site_state FROM posts WHERE id=?", (pid,)).fetchone()
        if cur and cur[0] == state:   # already known from an earlier sync
            return
        self._mark_post(pid, state, ts)
        if pid not in self._posts_lost:
            self._posts_lost.append(pid)
        self.arch.commit()
        self._check_posts()

    def _mark_post(self, pid: int, state: str, ts: int) -> None:
        a = self.arch
        a.db.execute("UPDATE posts SET site_state=?, site_state_at=? WHERE id=?", (state, ts, pid))
        record_event(a.db, "post", pid, "removed", state, ts, pid)
        path = a.raw_post(pid)
        title = ""
        raw = load_raw_post(a, pid)
        if raw is not None:
            title = raw.get("title") or ""
            if not raw.get("_site"):
                kept = keep(raw, state, ts)
                integrity.note(a.db, a, path, write_json_gz(path, kept), kept)
        self.site[f"posts_{state}"] += 1
        self.site_items.append({"kind": "post", "id": pid, "state": state, "title": short(title, 120)})
        log.warning(f"[посты] {pid} «{short(title, 60)}»: {state_title(state)} — в архиве остаётся сохранённая версия")

    def _context_kept(self, st: Counter) -> None:
        """Other people's comments that DTF lost inside archived trees and branches (their archived text is kept)."""
        n = st.get("kept", 0) + st.get("gone", 0) - st.get("mine", 0)
        if n > 0:
            self.site["context"] += n

    # ------------------------------------------------------------------ my comments
    def _upsert_comments(self, items: list[dict], dirty_years: set[str],
                         seen: set[int] | None = None) -> tuple[int, int]:
        """Store the user's comments. One that turned into a placeholder (or was wiped to ".") keeps its archived
        version with a `_site` mark; too many such losses in one sync stop it (GuardTrip)."""
        db = self.arch.db
        uid = self.arch.get_meta("user_id")
        ts = now_ts()
        new = 0
        n_media = 0
        rep = self._repairs.setdefault("comment", {})
        for c in items:
            author = (c.get("author") or {}).get("id")
            if author is not None and author != uid:
                continue
            if seen is not None:
                seen.add(c["id"])
            entry = c.get("entry") or {}
            cur = db.execute("SELECT raw, site_state FROM my_comments WHERE id=?", (c["id"],)).fetchone()
            if cur is None:
                new += 1
            else:
                old = safe_unpack(cur["raw"])
                if old is None:   # damaged and not repaired (the check was off): the fresh copy is all there is
                    old = {"_damaged": True}
                    integrity.queue(db, "comment", c["id"], None, None, "damaged")
                    rep.setdefault(c["id"], {})
                if not old.get("_damaged"):
                    why = degraded(old, c)
                    if why:
                        if not cur["site_state"]:
                            self._mark_comment(old, why, ts, dirty_years)
                        continue
                    record_edit(db, "comment", old, c, ts, entry.get("id"))
                    if cur["site_state"]:   # it is on DTF again
                        record_event(db, "comment", c["id"], "restored", None, ts, entry.get("id"))
            db.execute(
                "INSERT INTO my_comments(id,entry_id,date,level,reply_to,reply_count,thread_id,last_mod,is_removed,raw,seen_at,sig) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET entry_id=excluded.entry_id, "
                "date=excluded.date, level=excluded.level, reply_to=excluded.reply_to, reply_count=excluded.reply_count, "
                "thread_id=excluded.thread_id, last_mod=excluded.last_mod, is_removed=excluded.is_removed, "
                "raw=excluded.raw, seen_at=excluded.seen_at, sig=excluded.sig, site_state=NULL, site_state_at=NULL",
                (c["id"], entry.get("id"), c.get("date"), c.get("level", 0), c.get("replyTo") or 0,
                 c.get("replyCount", 0), c.get("threadId"), c.get("lastModificationDate"),
                 1 if c.get("isRemoved") else 0, pack(c), ts, content_sig("comment", c)))
            if c["id"] in rep:
                integrity.settle(self.arch, "comment", c["id"], c, entry.get("id"))
                rep.pop(c["id"], None)
            if c.get("date"):
                self._dirty(dirty_years, ts_year(c["date"]))
            if c.get("media"):
                n_media += self.arch.queue_media(collect_media(c["media"]), f"mc:{c['id']}")
        self._check_comments()
        return new, n_media

    def _mark_comment(self, old: dict, state: str, ts: int, dirty_years: set[str]) -> None:
        self.arch.db.execute("UPDATE my_comments SET site_state=?, site_state_at=?, raw=? WHERE id=?",
                             (state, ts, pack(keep(old, state, ts)), old["id"]))
        record_event(self.arch.db, "comment", old["id"], "removed", state, ts, (old.get("entry") or {}).get("id"))
        if old.get("date"):
            self._dirty(dirty_years, ts_year(old["date"]))
        self.site[f"comments_{state}"] += 1
        self._comments_lost += 1
        self.site_items.append({"kind": "comment", "id": old["id"], "state": state, "title": short(old.get("text"), 120),
                                "post": short((old.get("entry") or {}).get("title"), 80)})
        if len(self._comment_samples) < 10:
            e = old.get("entry") or {}
            self._comment_samples.append({"id": old["id"], "title": short(old.get("text"), 120) or "(без текста)",
                                          "post": short(e.get("title"), 80),
                                          "url": comment_url(e.get("id"), old["id"])})
        log.warning(f"[комментарии] {old['id']}: {state_title(state)} — в архиве остаётся сохранённый текст")

    def _check_comments(self) -> None:
        if self._comments_lost < COMMENTS_LIMIT or "comments-mass" in self.accepted:
            return
        self.arch.commit()
        raise GuardTrip(
            "comments-mass",
            f"За эту синхронизацию на DTF пропали или стёрты уже "
            f"{count_label(self._comments_lost, *COMMENTS)} (порог — {COMMENTS_LIMIT}). "
            f"Синхронизация остановлена, сохранённые тексты в архиве не изменены.",
            {"lost": self._comments_lost, "limit": COMMENTS_LIMIT, "samples": self._comment_samples})

    def _mark_gone_comments(self, seen: set[int], lo: int, hi: int, dirty_years: set[str]) -> None:
        """The user's comments dated inside the part of the feed just read, but missing from it: deleted by the
        user or a moderator. The feed pages by date, so `lo` itself may continue on the next page and is skipped."""
        ts = now_ts()
        rows = self.arch.db.execute("SELECT id, raw FROM my_comments WHERE date > ? AND date <= ? AND site_state IS NULL",
                                    (lo, hi)).fetchall()
        for r in rows:
            if r["id"] in seen:
                continue
            old = safe_unpack(r["raw"])
            if old is not None and not old.get("_damaged") and not comment_stub(old):
                self._mark_comment(old, "gone", ts, dirty_years)
        self._check_comments()

    def _dirty(self, dirty_years: set[str], year: str) -> None:
        """A year of raw/my-comments to export again — remembered in the database too, so a sync that stops before
        exporting leaves it for the next one (integrity.export_years)."""
        if year in dirty_years:
            return
        dirty_years.add(year)
        self.arch.set_meta(integrity.META_EXPORT_DIRTY,
                           sorted(set(self.arch.get_meta(integrity.META_EXPORT_DIRTY) or []) | {year}))

    def _repair_comments(self, dirty: set[str], counters: dict) -> None:
        """Own comments no local copy restored exactly (integrity.py): downloaded one by one with their branch and
        compared with the version the archive had (settle via _upsert_comments)."""
        rep = self._repairs.get("comment") or {}
        for cid in sorted(rep):
            self.check_cancel()
            try:
                items = self.api.comment_branch(cid)
            except FatalNetworkError:
                raise
            except Exception as e:  # noqa: BLE001
                if is_not_found(e):
                    row = self.arch.db.execute("SELECT entry_id FROM my_comments WHERE id=?", (cid,)).fetchone()
                    integrity.settle(self.arch, "comment", cid, None, row[0] if row else None)
                    rep.pop(cid, None)
                else:
                    integrity.attempt_failed(self.arch.db, "comment", cid)
                    log.warning(f"[комментарии] {cid}: не удалось скачать для восстановления: {e}")
                continue
            mine = [c for c in items if c.get("id") == cid]
            if mine:
                new, nm = self._upsert_comments(mine, dirty)
                counters["media"] += nm
            else:
                row = self.arch.db.execute("SELECT entry_id FROM my_comments WHERE id=?", (cid,)).fetchone()
                integrity.settle(self.arch, "comment", cid, None, row[0] if row else None)
                rep.pop(cid, None)
            self.arch.commit()

    def stage_comments(self, pool: ThreadPoolExecutor) -> None:
        a, db = self.arch, self.arch.db
        dirty: set[str] = set()
        counters = {"new": 0, "media": 0, "pages": 0}

        if self.full:
            db.execute("UPDATE slices SET done=0, last_id=NULL, last_sv=NULL, pages=0")
            a.set_meta("comments_backfill_done", None)
            a.commit()

        try:
            self._comments_stages(pool, dirty, counters)
            if self._repairs.get("comment"):
                self._repair_comments(dirty, counters)
        finally:   # even when the guard stops the sync: the raw files show the kept versions
            self._export_my_comments(dirty)
        total = db.execute("SELECT COUNT(*) FROM my_comments").fetchone()[0]
        self.stats["comments"] = {"total": total, **counters}
        self.report("comments", status="done", comments=total, new=counters["new"])
        log.info(f"[комментарии] готово: всего {total}, новых {counters['new']}, медиа в очередь +{counters['media']}")

    def _comments_stages(self, pool: ThreadPoolExecutor, dirty: set[str], counters: dict) -> None:
        a, db = self.arch, self.arch.db
        uid = a.get_meta("user_id")
        if not a.get_meta("comments_backfill_done"):
            self._make_slices()
            slices = {r["start"]: dict(r) for r in db.execute("SELECT * FROM slices WHERE done=0")}
            total = db.execute("SELECT COUNT(*) FROM slices").fetchone()[0]
            log.info(f"[комментарии] первичная выгрузка: {len(slices)} из {total} месячных срезов осталось")

            def work(start: int) -> tuple[list[dict], int | None, int | None]:
                s = slices[start]
                return self.api.user_comments_page(uid, s["last_id"] or FAR_ID, s["last_sv"] or s["end"])

            def done(start: int, res: Any, exc: Exception | None) -> list[int] | None:
                s = slices[start]
                if exc:
                    log.warning(f"[комментарии] срез {ts_month(start)}: {exc}; повторю при следующем запуске")
                    return None
                items, lid, lsv = res
                new, nm = self._upsert_comments(items, dirty)
                counters["new"] += new
                counters["media"] += nm
                counters["pages"] += 1
                finished = (not items or lid is None or min(c.get("date", 0) for c in items) < s["start"]
                            or (lid == s["last_id"] and lsv == s["last_sv"]))
                s["last_id"], s["last_sv"] = lid, lsv
                s["pages"] = (s.get("pages") or 0) + 1
                db.execute("UPDATE slices SET last_id=?, last_sv=?, pages=?, done=? WHERE start=?",
                           (lid, lsv, s["pages"], 1 if finished else 0, start))
                return None if finished else [start]

            def progress() -> dict:
                left = db.execute("SELECT COUNT(*) FROM slices WHERE done=0").fetchone()[0]
                n = db.execute("SELECT COUNT(*) FROM my_comments").fetchone()[0]
                return {"done": total - left, "total": total, "comments": n, "phase": "backfill"}

            # newest slices first: the most active period gets archived early
            self.run_parallel(pool, sorted(slices, reverse=True), work, done, "комментарии",
                              progress=progress, stage="comments")
            if db.execute("SELECT COUNT(*) FROM slices WHERE done=0").fetchone()[0] == 0:
                a.set_meta("comments_backfill_done", now_ts())
                a.commit()

        # head scan: newest comments + refresh reply counters inside the refresh window
        if a.get_meta("comments_backfill_done"):
            window = now_ts() - self.refresh_days * DAY
            lid = lsv = None
            pages = 0
            seen: set[int] = set()
            lo = hi = None
            while True:
                self.check_cancel()
                self.report("comments", status="running", phase="head", pages=pages,
                            comments=db.execute("SELECT COUNT(*) FROM my_comments").fetchone()[0])
                items, lid, lsv = self.api.user_comments_page(uid, lid, lsv)
                new, nm = self._upsert_comments(items, dirty, seen)
                dates = [c["date"] for c in items if c.get("date")]
                if dates:
                    lo = min(dates) if lo is None else min(lo, min(dates))
                    hi = max(dates) if hi is None else max(hi, max(dates))
                counters["new"] += new
                counters["media"] += nm
                pages += 1
                a.commit()
                if not items or lid is None:
                    if lo is not None:
                        lo -= 1          # the whole feed was read: its oldest comment is covered too
                    break
                if new == 0 and min(c.get("date", 0) for c in items) < window:
                    break
            if lo is not None and hi is not None:
                self._mark_gone_comments(seen, lo, hi, dirty)
            log.info(f"[комментарии] проверка свежих: {pages} стр.")

    def _make_slices(self) -> None:
        db = self.arch.db
        created = self.arch.get_meta("user_created") or 1262304000
        start = _dt.datetime.fromtimestamp(created, MSK).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_limit = _dt.datetime.now(MSK) + _dt.timedelta(days=2)
        existing = {r[0] for r in db.execute("SELECT start FROM slices")}
        cur = start
        while cur < end_limit:
            nxt = (cur.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
            s, e = int(cur.timestamp()), int(nxt.timestamp())
            if nxt > end_limit:
                e = int(end_limit.timestamp()) + DAY * 365  # the open-ended newest slice
            if s not in existing:
                db.execute("INSERT INTO slices(start,end) VALUES(?,?)", (s, e))
            elif nxt > end_limit:
                db.execute("UPDATE slices SET end=? WHERE start=?", (e, s))
            cur = nxt
        self.arch.commit()

    def _export_my_comments(self, dirty_years: set[str]) -> None:
        years = set(dirty_years) | set(self.arch.get_meta(integrity.META_EXPORT_DIRTY) or [])
        if not years:
            return
        integrity.export_my_comments(self.arch, years)
        log.info(f"[комментарии] сырые данные обновлены за годы: {', '.join(sorted(years))}")

    # ------------------------------------------------------------------ context threads
    def stage_threads(self, pool: ThreadPoolExecutor) -> None:
        a, db = self.arch, self.arch.db
        uid = a.get_meta("user_id")
        own = {r[0] for r in db.execute("SELECT id FROM posts")}
        need: dict[int, list[tuple[int, int, str | None]]] = defaultdict(list)
        for r in db.execute("SELECT id, entry_id, date, level, reply_count, thread_id FROM my_comments "
                            "WHERE entry_id IS NOT NULL AND (level > 0 OR reply_count > 0)"):
            if r["entry_id"] not in own:
                need[r["entry_id"]].append((r["id"], r["date"] or 0, r["thread_id"]))
        threads = {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM threads")}
        now = now_ts()
        window = now - self.refresh_days * DAY
        tasks: list[tuple[int, list[int], int]] = []
        for eid, lst in need.items():
            ids = sorted(x[0] for x in lst)
            n_threads = len({x[2] or f"c{x[0]}" for x in lst})
            th = threads.get(eid)
            max_date = max(x[1] for x in lst)
            if (self.full or th is None or max(ids) > (th["max_my_comment_id"] or 0) or th["status"] == "damaged"
                    or (th["status"] == "error" and (th["attempts"] or 0) < 6)
                    or (th["status"] == "ok" and max_date >= window and (th["fetched_at"] or 0) < now - 6 * HOUR)):
                tasks.append((eid, ids, n_threads))
        # newest discussions first
        tasks.sort(key=lambda t: -t[1][-1])
        log.info(f"[контекст] постов с комментариями пользователя, где нужен контекст: {len(need)}; к загрузке: {len(tasks)}")

        def work(task: tuple[int, list[int], int]) -> dict:
            eid, ids, n_threads = task
            mode = "branch"
            try:
                if n_threads == 1:
                    items = self.api.comment_branch(ids[-1])
                    got = {c["id"] for c in items}
                    if not all(i in got for i in ids):
                        items = self.api.post_comments(eid)
                        mode = "tree"
                else:
                    items = self.api.post_comments(eid)
                    mode = "tree"
            except Exception as e:
                if is_not_found(e):
                    return {"status": "gone", "error": str(e)[:300], "mode": mode}
                raise
            # a branch answer says nothing about the post's other threads: only those threads are compared
            threads = {c.get("threadId") for c in items} if mode == "branch" else None
            tree = self._write_tree(a.raw_thread(eid), items, uid, {"entryId": eid, "mode": mode, "myCommentIds": ids},
                                    threads=threads, prune=ids)
            st = tree["st"]
            kept_n = st.pop("_kept")
            missing = st.pop("_missing")
            return {"status": "ok", "mode": mode, "n_items": len(tree["merged"]), "n_kept": kept_n,
                    "missing": len(missing), "st": st, "tree": tree}

        stats = defaultdict(int)

        def done(task: tuple[int, list[int], int], res: dict | None, exc: Exception | None) -> None:
            eid, ids, _ = task
            ts = now_ts()
            if exc:
                stats["error"] += 1
                db.execute("INSERT INTO threads(entry_id,status,error,attempts) VALUES(?,?,?,1) "
                           "ON CONFLICT(entry_id) DO UPDATE SET status=CASE WHEN threads.status='damaged' THEN "
                           "'damaged' ELSE 'error' END, error=excluded.error, "
                           "attempts=COALESCE(threads.attempts,0)+1", (eid, "error", str(exc)[:500]))
                integrity.attempt_failed(db, "thread", eid)
                return
            stats[res["status"]] += 1
            stats["missing"] += res.get("missing", 0)
            if res.get("st"):
                self._context_kept(res["st"])
            if res.get("tree"):
                self._save_tree(res["tree"], ts, eid)
                integrity.settle_tree(a, "thread", eid, res["tree"]["events"], eid)
            elif eid in self._repairs.get("thread", {}):   # the post is gone from DTF: what survived stays
                integrity.settle_tree(a, "thread", eid, [], eid)
            db.execute(
                "INSERT INTO threads(entry_id,fetched_at,max_my_comment_id,mode,n_items,n_kept,missing,status,error,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,?,0) ON CONFLICT(entry_id) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "max_my_comment_id=excluded.max_my_comment_id, mode=excluded.mode, n_items=excluded.n_items, "
                "n_kept=excluded.n_kept, missing=excluded.missing, status=excluded.status, error=excluded.error, attempts=0",
                (eid, ts, max(ids), res.get("mode"), res.get("n_items"), res.get("n_kept"), res.get("missing"),
                 res["status"], res.get("error")))

        self.run_parallel(pool, tasks, work, done, "контекст", total=len(tasks), stage="threads",
                          progress=lambda: {"ok": stats["ok"], "gone": stats["gone"], "errors": stats["error"]})
        self.stats["threads"] = dict(stats)
        log.info(f"[контекст] готово: {dict(stats)}")

    # ------------------------------------------------------------------ media
    def _queue_from_raw(self) -> None:
        """From raw trees/threads changed since the last scan: small avatars of every comment author, and the media of
        other people's comments kept as context, so discussions show their pictures offline too."""
        a = self.arch
        if self.scope == "posts":   # no comments, no comment authors
            return
        since = a.get_meta("raw_scanned_mtime", 0)   # was avatars_scanned_mtime: a new key rescans once for context media
        newest = since
        seen: set[int] = set()
        avatars = context = 0
        for d in ("post-trees", "threads"):
            for p in (a.raw / d).glob("*.json.gz"):
                mt = p.stat().st_mtime
                if mt <= since:
                    continue
                try:
                    items = read_raw(p).get("items", [])
                except (Damaged, FileNotFoundError):
                    continue   # integrity.py repairs it; a new write gives it a new mtime
                newest = max(newest, mt)
                for c in items:
                    if d == "threads" and c.get("media"):   # tree media are queued by the posts stage
                        context += a.queue_media(collect_media(c["media"]), f"tc:{c.get('id')}")
                    au = c.get("author") or {}
                    uid = au.get("id")
                    if uid is None or uid in seen:
                        continue
                    seen.add(uid)
                    key = avatar_key(au.get("avatar"))
                    if key:
                        avatars += a.queue_media([(key, None, "jpg")], f"avatar:{uid}")
        a.set_meta("raw_scanned_mtime", newest)
        a.commit()
        if avatars or context:
            log.info(f"[медиа] в очередь: аватарок авторов +{avatars}, медиа из веток обсуждений +{context}")

    def stage_media(self, pool: ThreadPoolExecutor) -> None:
        a, db = self.arch, self.arch.db
        tmp = a.media / ".tmp" / a.nick
        shutil.rmtree(tmp, ignore_errors=True)  # partial downloads of this archive's interrupted run
        self._queue_from_raw()
        self.report("media", status="running", phase="prepare")
        retry_missing = " OR r.status='missing'" if self.full else ""
        todo = f"(r.status='pending' OR (r.status='error' AND r.attempts < 8){retry_missing})"
        wanted = f"EXISTS (SELECT 1 FROM main.media_use u WHERE u.key = r.key AND {owner_sql(self.media)})"
        # only the keys this archive needs (and its media mode allows); files other archives fetched are free
        rows = [dict(r) for r in db.execute(
            f"SELECT r.key, r.sig, r.kind, r.expect_sha FROM store.media_ref r WHERE {todo} AND {wanted} ORDER BY r.rowid")]
        skipped = 0 if self.media == "all" else db.execute(
            f"SELECT COUNT(*) FROM store.media_ref r WHERE {todo} AND NOT {wanted} AND "
            f"EXISTS (SELECT 1 FROM main.media_use u WHERE u.key = r.key)").fetchone()[0]
        shared = db.execute("SELECT COUNT(DISTINCT u.key) FROM main.media_use u JOIN store.media_ref r ON r.key=u.key "
                            "WHERE r.status='done'").fetchone()[0]
        by_sig: dict[str, list[dict]] = defaultdict(list)
        for b in db.execute("SELECT sha256, size, path, sig FROM store.blob WHERE sig IS NOT NULL"):
            by_sig[b["sig"]].append(dict(b))
        log.info(f"[медиа] к загрузке: {len(rows)} файлов (уже в хранилище: {shared})"
                 + (f"; медиа комментариев не скачивается по настройке архива: {skipped}" if skipped else ""))
        dl = Downloader(self.client, a.media, tmp)
        st = defaultdict(int)
        found: list[dict] = []

        def work(r: dict) -> dict:
            return dl.fetch(r["key"], r["kind"], list(by_sig.get(r["sig"], [])) if r["sig"] else [])

        def done(r: dict, res: dict | None, exc: Exception | None) -> None:
            ts = now_ts()
            if exc or res is None:
                res = {"status": "error", "error": str(exc)[:300]}
            status = res["status"]
            if status == "done":
                st[res["via"]] += 1
                if "path" in res:
                    st["bytes"] += res["size"] if res["via"] == "download" else 0
                    db.execute("INSERT OR IGNORE INTO store.blob(sha256,size,ext,mime,sig,path) VALUES(?,?,?,?,?,?)",
                               (res["sha256"], res["size"], res["ext"], res["mime"], r["sig"], res["path"]))
                    if r["sig"] and res["via"] == "download":
                        by_sig[r["sig"]].append({"sha256": res["sha256"], "size": res["size"], "path": res["path"]})
                db.execute("UPDATE store.media_ref SET status='done', sha256=?, via=?, error=NULL, updated_at=?, "
                           "expect_sha=NULL WHERE key=?", (res["sha256"], res["via"], ts, r["key"]))
                if r.get("expect_sha"):   # downloaded again after damage (integrity.py)
                    found.append(integrity.finding("media", r["key"], "damaged",
                                                   "media-exact" if res["sha256"] == r["expect_sha"] else "media-other"))
            elif status == "missing":
                st["missing"] += 1
                db.execute("UPDATE store.media_ref SET status='missing', error=?, updated_at=?, expect_sha=NULL "
                           "WHERE key=?", (res.get("error"), ts, r["key"]))
                if r.get("expect_sha"):
                    found.append(integrity.finding("media", r["key"], "damaged", "media-lost"))
            else:
                st["error"] += 1
                db.execute("UPDATE store.media_ref SET status='error', error=?, attempts=attempts+1, updated_at=? "
                           "WHERE key=?", (res.get("error"), ts, r["key"]))

        try:
            self.run_parallel(pool, rows, work, done, "медиа", total=len(rows), width=self.media_workers, stage="media",
                              progress=lambda: {"downloaded": st["download"], "bytes": st["bytes"],
                                                "dedup": st["dedup"] + st["probe"], "shared": shared,
                                                "missing": st["missing"], "errors": st["error"]})
        finally:
            integrity.log_findings(a, found)
            a.commit()
        self.stats["media"] = dict(st, mode=self.media, skipped_by_mode=skipped)
        log.info(f"[медиа] готово: {dict(st)}")


def live_stats(p: dict) -> dict:
    """What changes on a post without an edit: counters, reactions, donations, gifts (from the listing)."""
    out: dict[str, Any] = {}
    for k in ("counters", "reactions", "donations", "likes"):
        if isinstance(p.get(k), dict):
            out[k] = p[k]
    if isinstance(p.get("gifts"), list):
        out["gifts"] = len(p["gifts"])
    return out


_HELD: set[str] = set()   # locks held by this process
_HELD_LOCK = threading.Lock()


def acquire_lock(lock) -> int:
    """Take the archive's sync lock atomically. Returns 0, or the PID of the live process that holds it."""
    key = str(lock)
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                other = 0
            with _HELD_LOCK:
                held_here = key in _HELD
            if other and (held_here if other == os.getpid() else _pid_alive(other)):
                return other
            try:  # stale lock of a crashed run
                lock.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return other or -1
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        with _HELD_LOCK:
            _HELD.add(key)
        return 0
    return -1


def release_lock(lock) -> None:
    with _HELD_LOCK:
        _HELD.discard(str(lock))
    lock.unlink(missing_ok=True)


def lock_holder(lock) -> int:
    """PID of the live process syncing an archive (0 if none)."""
    try:
        other = int(lock.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0
    if other == os.getpid():
        with _HELD_LOCK:
            return other if str(lock) in _HELD else 0
    return other if other and _pid_alive(other) else 0


def _drain(pools: list[ThreadPoolExecutor], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for p in pools:
        for t in list(getattr(p, "_threads", ())):
            t.join(max(0.0, deadline - time.monotonic()))


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    if sys.platform.startswith("linux"):
        # a container has few PIDs and threads take ids from the same space: a stale lock of `docker exec ... sync`
        # must not look alive because a thread of the app (or a healthcheck) now has that number
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
            tgid = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("Tgid:")), 0)
            return tgid == pid and b"dtf_backup" in Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------- status
MEDIA_STATES = {"done": "скачано", "pending": "ждут загрузки", "error": "не скачались", "missing": "удалены на DTF"}
THREAD_STATES = {"ok": "загружено", "gone": "пост удалён на DTF", "error": "не загрузились",
                 "damaged": "ждут восстановления"}


def status(arch: Archive) -> str:
    """The archive in plain words (the sync tab's «Технические подробности», `status` in the console)."""
    if not arch.exists():
        return "Архив ещё не синхронизирован."
    db = arch.db
    one = lambda q, *p: db.execute(q, p).fetchone()[0]  # noqa: E731
    kept = arch.settings()["scope"] != "posts"
    uri = (arch.get_meta("user_uri") or "").strip("/")
    lines = [f"Папка: {arch.root}",
             f"Пользователь: {arch.get_meta('user_name')} (id {arch.get_meta('user_id')}" + (f", @{uri})" if uri else ")"),
             "Что сохраняется: " + ("посты и комментарии" if kept else "только посты")]
    g = arch.get_meta(META_GUARD)
    if g:
        lines.append(f"Синхронизация остановлена защитой архива {ts_human(g.get('ts'))}: {g.get('message')}")
        for x in (g.get("details") or {}).get("samples") or []:
            lines.append(f"  - {x.get('title')} {x.get('url') or ''}")
    last = arch.get_meta(META_LAST_SYNC) or {}
    if last:
        lines.append(f"Последняя синхронизация: {ts_human(last['finished'])}")
    lines.append("")
    n_perr = one("SELECT COUNT(*) FROM posts WHERE content_status='error' OR tree_status='error'")
    n_ok = one("SELECT COUNT(*) FROM posts WHERE content_status='ok'")
    n_dmg = one("SELECT COUNT(*) FROM repairs")
    lines.append(f"Посты: {num(one('SELECT COUNT(*) FROM posts'))}, загружено {num(n_ok)}"
                 + (f", с ошибкой {n_perr}" if n_perr else ""))
    chk = arch.get_meta(integrity.META_CHECK)
    if chk:
        lines.append(f"Проверка целостности: {ts_human(chk['ts'])}, " + (
            "всё в порядке" if not chk.get("problems") else
            f"найдено {chk['problems']}, восстановлено {chk.get('restored', 0)}, утрачено {chk.get('lost', 0)}")
            + (f"; ждут загрузки с DTF: {num(n_dmg)}" if n_dmg else ""))
    lost_p = one("SELECT COUNT(*) FROM posts WHERE site_state IS NOT NULL")
    lost_c = one("SELECT COUNT(*) FROM my_comments WHERE site_state IS NOT NULL")
    if lost_p or lost_c:
        lines.append(f"Удалено на DTF, но сохранено в архиве: постов {num(lost_p)}, комментариев {num(lost_c)}")
    if kept:
        n_trees = one("SELECT COUNT(*) FROM posts WHERE tree_status='ok'")
        lines.append(f"Комментарии под постами: загружено деревьев {num(n_trees)}")
        s_total, s_done = one("SELECT COUNT(*) FROM slices"), one("SELECT COUNT(*) FROM slices WHERE done=1")
        first = ("завершена" if arch.get_meta("comments_backfill_done") else
                 f"{num(s_done)} из {num(s_total)} месяцев" if s_total else "ещё не начата")
        lines.append(f"Комментарии пользователя: {num(one('SELECT COUNT(*) FROM my_comments'))} (первая выгрузка: {first})")
        th = {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM threads GROUP BY status")}
        lines.append(f"Ветки обсуждений на чужих постах: {num(sum(th.values()))}"
                     + (" (" + ", ".join(f"{THREAD_STATES.get(k, k)} {num(v)}" for k, v in sorted(th.items())) + ")"
                        if th else "")
                     + f"; своих комментариев, не найденных в ветках: {num(one('SELECT COALESCE(SUM(missing),0) FROM threads'))}")
        ctx = ((last.get("stats") or {}).get("site") or {}).get("context")
        if ctx:
            lines.append(f"  чужих комментариев в ветках удалено на DTF за последнюю синхронизацию: {num(ctx)} "
                         f"(в архиве они остались)")
    else:
        lines.append("Комментарии: не сохраняются (архив «Только посты»)")
    mine = "r.key IN (SELECT key FROM main.media_use)"
    media = {r[0]: r[1] for r in db.execute(f"SELECT r.status, COUNT(*) FROM store.media_ref r WHERE {mine} GROUP BY r.status")}
    dedup = one(f"SELECT COUNT(*) FROM store.media_ref r WHERE {mine} AND r.status='done' AND r.via IN ('probe','dedup')")
    blobs, size = arch.media_usage()
    all_blobs, all_size = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM store.blob").fetchone()
    lines.append(f"Медиафайлы: ссылок {num(sum(media.values()))}"
                 + (" — " + ", ".join(f"{MEDIA_STATES.get(k, k)} {num(v)}" for k, v in sorted(media.items())) if media else ""))
    lines.append(f"  файлов этого архива {num(blobs)}, {human_bytes(size)}; совпали с уже скачанными: {num(dedup)}")
    lines.append(f"  общее хранилище всех архивов: {num(all_blobs)} файлов, {human_bytes(all_size)} ({arch.media})")
    for e in db.execute(f"SELECT r.key, r.error FROM store.media_ref r WHERE {mine} AND r.status='error' LIMIT 5"):
        lines.append(f"  не скачался {e[0]}: {e[1]}")
    if arch.report_path.exists():
        rep = read_json(arch.report_path)
        uns = rep.get("unsupported", {})
        lines.append("")
        try:
            built = ts_human(int(_dt.datetime.fromisoformat(rep["renderedAt"]).timestamp()))
        except (KeyError, TypeError, ValueError):
            built = "?"
        lines.append(f"Последняя сборка страниц: {built}")
        if uns:
            lines.append("  Не показаны (неизвестный формат DTF): " + ", ".join(f"{k}×{v['count']}" for k, v in uns.items()))
        if rep.get("unknownReactions"):
            lines.append("  Реакции, которых нет в каталоге: " + ", ".join(f"#{k}×{v['count']}" for k, v in rep["unknownReactions"].items()))
        if rep.get("generic"):
            lines.append("  Показаны упрощённо: " + ", ".join(f"{k}×{v['count']}" for k, v in rep["generic"].items()))
    return "\n".join(lines)


def read_json(path) -> Any:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def load_raw_post(arch: Archive, pid: int) -> dict | None:
    """The archived post; None if there is none or its file is damaged (integrity.py repairs it)."""
    try:
        return read_raw(arch.raw_post(pid), None)
    except Damaged:
        return None
