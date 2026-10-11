"""Archive integrity: what LDTF stores is checked against what it wrote, and repaired without ever passing a version DTF
has now off as the one the archive had.

Every raw file is in the manifest (state.sqlite `files`: sha256, size, date of its content) the moment it is written.
A check runs first in every sync ("quick") and from «Проверить целостность» ("full"):
  raw/      — sha256 of every file against the manifest; a file that differs is read to tell a write whose manifest
              row a crash lost (newer content: adopted) from damage (unreadable) or a stale copy put back by another
              program (older content);
  database  — every own comment (my_comments.raw) and every kept version (history.body) unpacks; a full check also
              compares own comments with the signature they were stored with;
  media     — every file of the archive is there with its size; sha256 of about 1/7 of the bytes (the longest
              unchecked first), of all of them in a full check.
Repairs, local first:
  .state/backup/raw/   the mirror: exact bytes of raw/ as of the last good sync (refreshed by finish());
  view.sqlite          built from raw/ after every sync: whole posts, slim comments, the history;
  raw/my-comments/     the export of own comments (and the database for the export).
A copy with the signature of the stored version restores it exactly, quietly. Without one, the best surviving copy is
put in place and the item is downloaded again (`repairs`): the sync compares what DTF has now with the expected
signature (settle) — the same version: restored; another one: history event `damaged`/`replaced` (the surviving older
copy stays an ordinary version); DTF doesn't have it: `damaged`/`lost`.
Damaged files go to .state/quarantine/<time>/ (library/.state/quarantine/media/ for media), never deleted.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

from .history import content_sig, record_damaged
from .state import Archive, pack, unpack
from .util import (atomic_write_bytes, file_sha256, gz_json, log, now_ts, read_json_gz, replace_file, short, ts_year,
                   write_durable, write_jsonl_gz)

DAMAGE = (EOFError, OSError, ValueError, zlib.error)   # gzip.BadGzipFile is an OSError, JSON errors are ValueErrors
RAW_DIRS = {"posts": ".json.gz", "post-trees": ".json.gz", "threads": ".json.gz", "my-comments": ".jsonl.gz"}
RAW_FILES = ("profile.json.gz", "assets.json.gz")
MEDIA_SHARE = 7     # a quick check hashes ~1/7 of the archive's media bytes: daily syncs go over all of them in a week
MEDIA_MIN = 64 << 20
HASH_THREADS = 8    # files are small and many: opening them (antivirus included) costs more than hashing
MAX_ATTEMPTS = 3    # a download for a repair that failed in this many syncs ends as lost
LOG_KEEP = 300

META_CHECK = "integrity"           # the last check: {ts, mode, took, checked, problems, restored, pending, lost}
META_FULL = "integrity_full"       # the last check that hashed every media file: {ts, files, bytes}
META_BASELINE = "integrity_baseline"
META_LOG = "integrity_log"         # what checks and repairs found, newest first (the sync tab)
META_EXPORT_DIRTY = "export_dirty"  # years of raw/my-comments to write again (a sync that stopped before exporting)
META_DB_BACKUP = "db_backup"       # when .state/backup/state.sqlite was taken

OUTCOMES = {
    "restored": "восстановлено из локальной копии",
    "adopted": "файл новее записи о нём: принят",
    "exported": "записано заново из базы архива",
    "pending": "будет скачано с DTF при синхронизации",
    "exact": "скачано с DTF заново — совпадает с тем, что было в архиве",
    "replaced": "скачано с DTF заново — на DTF уже другая версия (в истории отмечено)",
    "reloaded": "ветка скачана с DTF заново",
    "lost": "восстановить не удалось: на DTF этого уже нет",
    "partial": "восстановлено из более ранней копии",
    "media-exact": "медиафайл скачан заново — совпадает",
    "media-other": "медиафайл скачан заново: на DTF сейчас другой файл, сохранён он",
    "media-lost": "медиафайл не восстановить: на DTF его больше нет",
}
PROBLEMS = {"damaged": "повреждён", "missing": "пропал с диска", "stale": "подменён старой копией",
            "changed": "изменён вне LDTF", "unreadable": "не читается"}


class Damaged(Exception):
    """A raw file that exists but can't be read: the integrity check repairs it, nothing may overwrite it blindly."""

    def __init__(self, path: Path, why: BaseException | str):
        super().__init__(f"повреждён файл {path.name}: {why}")
        self.path = path


class DbDamaged(Exception):
    """PRAGMA quick_check of a database failed (schema: main = the archive's state.sqlite, store = the shared
    media.sqlite), or one could not be restored."""

    def __init__(self, message: str, schema: str = "main"):
        super().__init__(message)
        self.schema = schema


_REQ: Any = object()


def read_raw(path: Path, default: Any = _REQ) -> Any:
    """read_json_gz that tells damage (Damaged) apart from a missing file (`default`, or FileNotFoundError)."""
    try:
        return read_json_gz(path)
    except FileNotFoundError:
        if default is _REQ:
            raise
        return default
    except DAMAGE as e:
        raise Damaged(path, e) from e


def decode(data: bytes, jsonl: bool = False) -> Any:
    text = gzip.decompress(data).decode("utf-8")
    if jsonl:
        return [json.loads(line) for line in text.split("\n") if line.strip()]
    return json.loads(text)


def safe_unpack(blob: bytes | None) -> dict | None:
    try:
        return unpack(blob) if blob else None
    except DAMAGE:
        return None


# ---------------------------------------------------------------------- manifest
def rel(arch: Archive, path: Path) -> str:
    return Path(path).resolve().relative_to(arch.raw).as_posix()


def content_at(relpath: str, obj: Any) -> int | None:
    """The date inside a raw file: a newer one means a newer write."""
    top = relpath.split("/")[0]
    if not isinstance(obj, dict):
        return None
    if top == "posts":
        return obj.get("dateModified") or obj.get("date")
    if top in ("post-trees", "threads") or relpath == "assets.json.gz":
        return obj.get("fetchedAt")
    return None


def note(db: sqlite3.Connection, arch: Archive, path: Path, sig: tuple[str, int], obj: Any = None) -> None:
    """Record a raw file just written (main thread; committed with the sync's other changes)."""
    r = rel(arch, path)
    db.execute("INSERT INTO files(path, sha256, size, at) VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
               "sha256=excluded.sha256, size=excluded.size, at=excluded.at", (r, sig[0], sig[1], content_at(r, obj)))


def forget(db: sqlite3.Connection, prefixes: Iterable[str]) -> None:
    for p in prefixes:
        db.execute("DELETE FROM files WHERE path LIKE ?", (p.rstrip("/") + "/%",))


def mirror_root(arch: Archive) -> Path:
    return arch.state_dir / "backup" / "raw"


def item_of(relpath: str) -> tuple[str, int] | None:
    """(kind of repair, id) of a raw file: posts/5.json.gz -> ("post", 5)."""
    top, _, name = relpath.partition("/")
    kind = {"posts": "post", "post-trees": "tree", "threads": "thread"}.get(top)
    if not kind:
        return None
    try:
        return kind, int(name.split(".")[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------- findings
def log_findings(arch: Archive, items: list[dict]) -> None:
    """Append to the archive's integrity log (meta, newest first)."""
    if not items:
        return
    old = arch.get_meta(META_LOG) or []
    arch.set_meta(META_LOG, (list(reversed(items)) + old)[:LOG_KEEP])


def finding(kind: str, ident: Any, problem: str, outcome: str, title: str = "", **extra: Any) -> dict:
    return {"at": now_ts(), "kind": kind, "id": ident, "problem": problem, "outcome": outcome,
            **({"title": title} if title else {}), **extra}


# ---------------------------------------------------------------------- the check
class Check:
    """One integrity check of an archive (holds its sync lock). mode: quick | full. `media`: check media files too."""

    def __init__(self, arch: Archive, mode: str = "quick", media: bool = True,
                 report: Callable[..., None] | None = None, cancel: Callable[[], None] | None = None):
        self.arch = arch
        self.db = arch.db
        self.full = mode == "full"
        self.mode = mode
        self.media = media
        self._report = report or (lambda **_: None)
        self.cancel = cancel or (lambda: None)
        self.found: list[dict] = []
        self.counts: Counter = Counter()
        self.reexport: set[str] = set()
        self._qdir: Path | None = None
        self._view: Any = None
        self._view_users: dict | None = None
        self._view_meta: dict | None = None
        self.started = now_ts()

    # --- plumbing
    def step(self, phase: str, done: int = 0, total: int = 0) -> None:
        self._report(phase=phase, done=done, total=total, problems=len(self.found))

    def add(self, f: dict) -> None:
        self.found.append(f)
        log.warning(f"[целостность] {f['kind']} {f.get('id')}: {PROBLEMS.get(f['problem'], f['problem'])} — "
                    f"{OUTCOMES.get(f['outcome'], f['outcome'])}")

    def quarantine(self, path: Path) -> None:
        if self._qdir is None:
            self._qdir = quarantine_dir(self.arch)
        set_aside(self.arch, path, self._qdir)

    def put(self, relpath: str, data: bytes, obj: Any) -> None:
        """Replace a raw file with good bytes (the old one goes to quarantine) and record it."""
        p = self.arch.raw / relpath
        self.quarantine(p)
        sig = write_durable(p, data)
        note(self.db, self.arch, p, sig, obj)

    def view(self) -> Any:
        if self._view is None:
            from .viewdb import open_view
            self._view = open_view(self.arch) or False
        return self._view or None

    def view_users(self) -> dict:
        if self._view_users is None:
            v = self.view()
            self._view_users = {r[0]: {"name": r[1], "nickname": r[2], "uri": r[3], "avatar": r[4]}
                                for r in v.execute("SELECT id, name, nickname, uri, avatar FROM users")} if v else {}
        return self._view_users

    def view_built(self) -> int:
        if self._view_meta is None:
            v = self.view()
            try:
                self._view_meta = {r[0]: json.loads(r[1]) for r in v.execute("SELECT key, value FROM meta")} if v else {}
            except (sqlite3.Error, ValueError):
                self._view_meta = {}
        return int(self._view_meta.get("built_at") or 0)

    def close(self) -> None:
        if self._view:
            self._view.close()
        self._view = None

    # --- run
    def run(self) -> "Check":
        t0 = time.monotonic()
        try:
            self.step("db")
            quick_check(self.db, "main")
            quick_check(self.db, "store")
            # keys this archive uses that the shared catalog lost (a restored media.sqlite): downloaded again
            self.db.execute("INSERT OR IGNORE INTO store.media_ref(key, status, updated_at) "
                            "SELECT DISTINCT key, 'pending', ? FROM main.media_use", (now_ts(),))
            if self.arch.get_meta(META_BASELINE) is None:
                self.baseline()
            self.check_files()
            self.check_comments()
            self.check_history()
            self.export_years()
            self.db.commit()
            if self.media:
                self.check_media()
        finally:
            self.close()
        self.save(time.monotonic() - t0)
        return self

    def summary(self) -> dict:
        c = Counter(f["outcome"] for f in self.found)
        return {"checked": dict(self.counts), "problems": len(self.found),
                "restored": c["restored"] + c["adopted"] + c["exported"] + c["partial"],
                "pending": c["pending"], "lost": c["lost"] + c["media-lost"]}

    def save(self, took: float) -> None:
        a = self.arch
        a.set_meta(META_CHECK, {"ts": self.started, "mode": self.mode, "took": round(took, 1), "media": self.media,
                                **self.summary()})
        if self.full and self.media:
            a.set_meta(META_FULL, {"ts": self.started, "files": self.counts["media_hashed"],
                                   "bytes": self.counts["media_bytes"]})
        log_findings(a, [f for f in self.found if f["outcome"] != "adopted"])
        a.commit()
        s = self.summary()
        log.info(f"[целостность] проверено за {took:.0f} с: файлов {self.counts['files']}, своих комментариев "
                 f"{self.counts['comments']}, версий {self.counts['versions']}, медиа {self.counts['media']} "
                 f"(целиком {self.counts['media_hashed']}); проблем {s['problems']}"
                 + (f", восстановлено {s['restored']}, к загрузке {s['pending']}, утрачено {s['lost']}"
                    if s["problems"] else ""))

    # --- baseline: the first check of an archive (LDTF before 1.6 kept no manifest)
    def raw_files(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        raw = self.arch.raw
        for name in RAW_FILES:
            p = raw / name
            if p.is_file():
                out[name] = p
        for d, ext in RAW_DIRS.items():
            if (raw / d).is_dir():
                for p in (raw / d).iterdir():
                    if p.is_file() and p.name.endswith(ext):
                        out[f"{d}/{p.name}"] = p
        return out

    def baseline(self) -> None:
        """Hash and read every raw file once: readable ones become the manifest (and the signatures of the stored
        versions); unreadable ones are left without a row — check_files repairs them, the pages' copy being the
        reference of what the archive had."""
        files = self.raw_files()
        log.info(f"[целостность] первая проверка архива: опись {len(files)} файлов")
        db = self.db
        for i, (r, p) in enumerate(sorted(files.items())):
            if i % 200 == 0:
                self.cancel()
                self.step("baseline", i, len(files))
            try:
                data = p.read_bytes()
                obj = decode(data, jsonl=r.endswith(".jsonl.gz"))
            except DAMAGE:
                continue
            note(db, self.arch, p, (hashlib.sha256(data).hexdigest(), len(data)), obj)
            if r.startswith("posts/") and isinstance(obj, dict) and obj.get("id"):
                db.execute("UPDATE posts SET content_sig=? WHERE id=?", (content_sig("post", obj), obj["id"]))
        self.arch.set_meta(META_BASELINE, now_ts())
        db.commit()

    # --- raw files
    def expected(self) -> set[str]:
        """Files the database says the archive has."""
        db = self.db
        out = {f"posts/{r[0]}.json.gz" for r in db.execute("SELECT id FROM posts WHERE content_status='ok'")}
        if self.arch.settings()["scope"] != "posts":
            out |= {f"post-trees/{r[0]}.json.gz" for r in db.execute("SELECT id FROM posts WHERE tree_status='ok'")}
            out |= {f"threads/{r[0]}.json.gz" for r in db.execute("SELECT entry_id FROM threads WHERE status='ok'")}
            out |= {f"my-comments/{ts_year(r[0] or 0)}.jsonl.gz"
                    for r in db.execute("SELECT DISTINCT date FROM my_comments WHERE date IS NOT NULL")}
        return out

    def check_files(self) -> None:
        db = self.db
        rows = {r["path"]: dict(r) for r in db.execute("SELECT path, sha256, size, at FROM files")}
        disk = self.raw_files()
        todo = sorted(set(rows) | set(disk) | self.expected())
        hashes = hash_files([disk[r] for r in todo if r in disk], self.cancel,
                            lambda i, n: self.step("files", i, n))
        for r in todo:
            row = rows.get(r)
            p = disk.get(r)
            if p is None:
                if r.startswith("my-comments/"):
                    self.reexport.add(r.split("/")[1].split(".")[0])
                    if row:
                        db.execute("DELETE FROM files WHERE path=?", (r,))
                    continue
                self.problem(r, row, "missing")
                continue
            sha, size = hashes.get(p, (None, 0))
            if sha is None:
                self.problem(r, row, "unreadable")
                continue
            self.counts["files"] += 1
            self.counts["bytes"] += size
            if row and sha == row["sha256"]:
                continue
            try:
                data = p.read_bytes()
                obj = decode(data, jsonl=r.endswith(".jsonl.gz"))
            except DAMAGE:
                self.problem(r, row, "damaged")
                continue
            if row is None or self.adoptable(r, row, obj):
                note(db, self.arch, p, (hashlib.sha256(data).hexdigest(), len(data)), obj)
                if r.startswith("posts/") and isinstance(obj, dict) and obj.get("id"):
                    db.execute("UPDATE posts SET content_sig=? WHERE id=?", (content_sig("post", obj), obj["id"]))
                if row is not None:
                    self.found.append(finding("file", r, "changed", "adopted"))
                continue
            self.problem(r, row, "stale", obj)
        db.commit()

    def adoptable(self, r: str, row: dict, obj: Any) -> bool:
        """A readable file that differs from its manifest row: a write whose row a crash lost (adopt) or an old copy
        put back by something else (repair)."""
        top = r.split("/")[0]
        if r == "profile.json.gz":
            return True
        if top == "my-comments":
            return False   # an export: written again from the database
        if top == "posts" and isinstance(obj, dict):
            cur = self.db.execute("SELECT content_sig FROM posts WHERE id=?", (obj.get("id"),)).fetchone()
            if cur and cur[0] and content_sig("post", obj) == cur[0]:
                return True   # the same version (counters differ)
        at = content_at(r, obj)
        return bool(at and at > (row.get("at") or 0))

    def problem(self, r: str, row: dict | None, kind: str, obj: Any = None) -> None:
        top = r.split("/")[0]
        if top == "my-comments":   # an export of the database: written again once damaged own comments are repaired
            self.reexport.add(r.split("/")[1].split(".")[0])
            if kind == "damaged":
                self.quarantine(self.arch.raw / r)
            return
        exact = self.mirror_bytes(r, row)
        if exact is not None:
            try:
                obj2 = decode(exact)
            except DAMAGE:
                obj2 = None
            if obj2 is not None:
                self.put(r, exact, obj2)
                if top == "posts" and isinstance(obj2, dict):
                    self.db.execute("UPDATE posts SET content_sig=? WHERE id=?",
                                    (content_sig("post", obj2), obj2.get("id")))
                self.add(finding("file", r, kind, "restored", source="копия"))
                return
        if top == "posts":
            self.repair_post(r, row, kind, obj)
        elif top in ("post-trees", "threads"):
            self.repair_tree(r, row, kind, obj)
        elif r == "profile.json.gz":
            prof = self.view_profile()
            if prof:
                self.put(r, gz_json(prof), prof)
                self.add(finding("file", r, kind, "restored", source="страницы"))
            else:
                self.quarantine(self.arch.raw / r)
                self.db.execute("DELETE FROM files WHERE path=?", (r,))
                self.add(finding("file", r, kind, "pending"))
        elif r == "assets.json.gz":   # the reaction catalog: the next sync downloads it again
            if obj is None:
                self.quarantine(self.arch.raw / r)
                self.db.execute("DELETE FROM files WHERE path=?", (r,))
            self.add(finding("file", r, kind, "pending"))

    def mirror_bytes(self, r: str, row: dict | None) -> bytes | None:
        """The mirror's copy of a file, if it is exactly what the manifest says the file was."""
        if not row:
            return None
        m = mirror_root(self.arch) / r
        try:
            data = m.read_bytes()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == row["sha256"] else None

    def mirror_obj(self, r: str) -> Any:
        try:
            return decode((mirror_root(self.arch) / r).read_bytes(), jsonl=r.endswith(".jsonl.gz"))
        except DAMAGE:
            return None

    def view_profile(self) -> dict | None:
        self.view_built()
        p = (self._view_meta or {}).get("profile")
        return p if isinstance(p, dict) and p.get("id") else None

    # --- posts
    def repair_post(self, r: str, row: dict | None, kind: str, stale: Any) -> None:
        db = self.db
        pid = item_of(r)[1]   # type: ignore[index]
        pr = db.execute("SELECT content_sig, site_state, date_modified FROM posts WHERE id=?", (pid,)).fetchone()
        cands: list[tuple[str, dict]] = []
        if isinstance(stale, dict):
            cands.append(("файл", stale))
        m = self.mirror_obj(r)
        if isinstance(m, dict):
            cands.append(("копия", m))
        v = self.view_post(pid)
        if v:
            cands.append(("страницы", v))
        expect = pr["content_sig"] if pr else None
        if expect is None and v:   # damaged before signatures were kept: the pages show what the archive had
            expect = content_sig("post", v)
        title = next((c.get("title") for _, c in cands if c.get("title")), "")
        exact = next(((s, c) for s, c in cands if expect and content_sig("post", c) == expect), None)
        if exact:
            self.put(r, gz_json(exact[1]), exact[1])
            db.execute("UPDATE posts SET content_sig=? WHERE id=?", (expect, pid))
            self.add(finding("post", pid, kind, "restored", short(title, 120), source=exact[0]))
            return
        best = max(cands, key=lambda c: (c[1].get("dateModified") or c[1].get("date") or 0, c[0] != "страницы"),
                   default=None)
        if best:
            self.put(r, gz_json(best[1]), best[1])
            db.execute("UPDATE posts SET content_sig=? WHERE id=?", (content_sig("post", best[1]), pid))
        else:
            self.quarantine(self.arch.raw / r)
            db.execute("DELETE FROM files WHERE path=?", (r,))
        expect_date = (row or {}).get("at") or (pr["date_modified"] if pr else None)
        if pr is not None and pr["site_state"]:   # deleted on DTF: nothing to download
            record_damaged(db, "post", pid, expect, expect_date, "lost", pid)
            self.add(finding("post", pid, kind, "partial" if best else "lost", short(title, 120)))
            return
        queue(db, "post", pid, expect, expect_date, kind)
        if pr is not None:
            db.execute("UPDATE posts SET content_status='damaged' WHERE id=?", (pid,))
        self.add(finding("post", pid, kind, "pending", short(title, 120)))

    def view_post(self, pid: int) -> dict | None:
        v = self.view()
        if not v:
            return None
        try:
            row = v.execute("SELECT raw FROM posts WHERE id=?", (pid,)).fetchone()
        except sqlite3.Error:
            return None
        p = safe_unpack(row[0]) if row else None
        if p:
            p.pop("_statsAt", None)
        return p

    # --- comment trees and discussion threads
    def repair_tree(self, r: str, row: dict | None, kind: str, stale: Any) -> None:
        db = self.db
        rk, xid = item_of(r)   # type: ignore[misc]
        cands: list[tuple[str, dict]] = []
        if isinstance(stale, dict):
            cands.append(("файл", stale))
        m = self.mirror_obj(r)
        if isinstance(m, dict):
            cands.append(("копия", m))
        if not cands:
            v = self.view_tree(rk, xid)
            if v:
                cands.append(("страницы", v))
        best = max(cands, key=lambda c: c[1].get("fetchedAt") or 0, default=None)
        if best and best[1] is not stale:
            self.put(r, gz_json(best[1]), best[1])
        elif not best:
            self.quarantine(self.arch.raw / r)
            db.execute("DELETE FROM files WHERE path=?", (r,))
        if rk == "tree":
            gone = db.execute("SELECT site_state FROM posts WHERE id=?", (xid,)).fetchone()
            gone = bool(gone and gone[0])
        else:
            st = db.execute("SELECT status FROM threads WHERE entry_id=?", (xid,)).fetchone()
            gone = bool(st and st[0] == "gone")
        what = "комментарии под постом" if rk == "tree" else "ветка обсуждения"
        if gone:   # the post is gone from DTF: what survived is all there is
            self.add(finding(rk, xid, kind, "partial" if best else "lost", what))
            return
        queue(db, rk, xid, None, (row or {}).get("at"), kind)
        if rk == "tree":
            db.execute("UPDATE posts SET tree_status='damaged' WHERE id=?", (xid,))
        else:
            db.execute("UPDATE threads SET status='damaged', attempts=0 WHERE entry_id=?", (xid,))
        self.add(finding(rk, xid, kind, "pending", what, source=best[0] if best else None))

    def view_tree(self, rk: str, xid: int) -> dict | None:
        """A comment tree rebuilt from the pages' slim copies (author names, texts, media, removal marks)."""
        v = self.view()
        if not v:
            return None
        try:
            rows = v.execute("SELECT data FROM comments WHERE entry_id=? ORDER BY date, id", (xid,)).fetchall()
        except sqlite3.Error:
            return None
        if not rows:
            return None
        users, built = self.view_users(), self.view_built()
        items = [unslim(c, users, built) for c in (safe_unpack(r[0]) for r in rows) if c]
        if rk == "tree":
            head: dict[str, Any] = {"postId": xid, "counter": None}
        else:
            mine = [r[0] for r in self.db.execute("SELECT id FROM my_comments WHERE entry_id=? ORDER BY id", (xid,))]
            head = {"entryId": xid, "mode": "tree", "myCommentIds": mine, "missing": []}
        return {**head, "fetchedAt": built, "restoredFrom": "view", "items": items}

    # --- own comments (my_comments.raw)
    def check_comments(self) -> None:
        db = self.db
        bad: list[tuple[sqlite3.Row, str]] = []
        rows = db.execute("SELECT id, raw, sig, date, entry_id FROM my_comments").fetchall()
        for i, row in enumerate(rows):
            if i % 5000 == 0:
                self.cancel()
                self.step("comments", i, len(rows))
            self.counts["comments"] += 1
            try:
                data = zlib.decompress(row["raw"])
                if self.full or row["sig"] is None:
                    c = json.loads(data.decode("utf-8"))
                    if c.get("_damaged"):
                        continue
                    sig = content_sig("comment", c)
                    if row["sig"] is None:
                        db.execute("UPDATE my_comments SET sig=? WHERE id=?", (sig, row["id"]))
                    elif sig != row["sig"]:
                        bad.append((row, "changed"))
            except (DAMAGE + (TypeError,)):
                bad.append((row, "damaged"))
        if not bad:
            return
        years = {ts_year(r["date"] or 0) for r, _ in bad}
        copies: dict[int, list[tuple[str, dict]]] = {}
        for y in years:
            for src, path in (("экспорт", self.arch.raw_my_comments(y)), ("копия", mirror_root(self.arch) / f"my-comments/{y}.jsonl.gz")):
                try:
                    items = decode(path.read_bytes(), jsonl=True)
                except DAMAGE:
                    continue
                for c in items:
                    if isinstance(c, dict) and "id" in c:
                        copies.setdefault(c["id"], []).append((src, c))
        users, built = self.view_users(), self.view_built()
        for row, kind in bad:
            cid = row["id"]
            cands = list(copies.get(cid, []))
            vc = self.view_comment(cid)
            if vc:
                cands.append(("страницы", unslim(vc, users, built)))
            expect = row["sig"]
            if expect is None and cands:   # never signed: the export is what the archive had
                expect = content_sig("comment", cands[0][1])
            text = next((c.get("text") for _, c in cands if c.get("text")), "")
            exact = next(((s, c) for s, c in cands if expect and content_sig("comment", c) == expect), None)
            self.reexport.add(ts_year(row["date"] or 0))
            if exact:
                db.execute("UPDATE my_comments SET raw=?, sig=? WHERE id=?", (pack(exact[1]), expect, cid))
                self.add(finding("comment", cid, kind, "restored", short(text, 120), source=exact[0]))
                continue
            best = max(cands, key=lambda c: (c[1].get("lastModificationDate") or c[1].get("date") or 0,
                                             c[0] != "страницы"), default=None)
            keep_obj = best[1] if best else {"id": cid, "date": row["date"], "entry": {"id": row["entry_id"]},
                                             "_damaged": True}
            db.execute("UPDATE my_comments SET raw=?, sig=? WHERE id=?",
                       (pack(keep_obj), content_sig("comment", keep_obj) if best else None, cid))
            state = db.execute("SELECT site_state FROM my_comments WHERE id=?", (cid,)).fetchone()
            if state and state[0]:   # deleted on DTF: nothing to download
                record_damaged(db, "comment", cid, expect, None, "lost", row["entry_id"])
                self.add(finding("comment", cid, kind, "partial" if best else "lost", short(text, 120)))
                continue
            queue(db, "comment", cid, expect, None, kind)
            self.add(finding("comment", cid, kind, "pending", short(text, 120)))
        db.commit()

    def view_comment(self, cid: int) -> dict | None:
        v = self.view()
        if not v:
            return None
        try:
            row = v.execute("SELECT data FROM comments WHERE id=?", (cid,)).fetchone()
        except sqlite3.Error:
            return None
        return safe_unpack(row[0]) if row else None

    def export_years(self) -> None:
        """raw/my-comments/<year> written again from the database: damaged or missing files, years of repaired
        comments, years a stopped sync left unexported."""
        years = set(self.reexport) | set(self.arch.get_meta(META_EXPORT_DIRTY) or [])
        if not years or self.arch.settings()["scope"] == "posts":
            return
        n = export_my_comments(self.arch, years)
        for y in sorted(years):
            if y in self.reexport:
                self.found.append(finding("file", f"my-comments/{y}.jsonl.gz", "damaged", "exported"))
        log.info(f"[целостность] экспорт своих комментариев записан заново за годы: {', '.join(sorted(years))} ({n})")

    # --- kept versions (history.body)
    def check_history(self) -> None:
        db = self.db
        bad = []
        rows = db.execute("SELECT id, kind, item_id, entry_id, sig, body FROM history WHERE body IS NOT NULL").fetchall()
        for i, row in enumerate(rows):
            if i % 5000 == 0:
                self.cancel()
                self.step("history", i, len(rows))
            self.counts["versions"] += 1
            try:
                body = zlib.decompress(row["body"])
                if self.full:
                    json.loads(body.decode("utf-8"))
            except (DAMAGE + (TypeError,)):
                bad.append(row)
        v = self.view() if bad else None
        for row in bad:
            good = None
            if v:
                try:
                    r = v.execute("SELECT body FROM history WHERE id=?", (row["id"],)).fetchone()
                except sqlite3.Error:
                    r = None
                b = safe_unpack(r[0]) if r else None
                if b and (not row["sig"] or content_sig(row["kind"], b) == row["sig"]):
                    good = r[0]
            src = "страницы"
            if good is None:
                good, src = backup_history_body(self.arch, row), "копия базы"
            if good is not None:
                db.execute("UPDATE history SET body=? WHERE id=?", (good, row["id"]))
                self.add(finding("version", row["id"], "damaged", "restored", source=src))
                continue
            # no copy: the version becomes a marker that it was lost
            cur = db.execute("UPDATE OR IGNORE history SET event='damaged', state='lost', body=NULL WHERE id=?",
                             (row["id"],))
            if not cur.rowcount:
                db.execute("DELETE FROM history WHERE id=?", (row["id"],))
            self.add(finding("version", row["id"], "damaged", "lost"))
        db.commit()

    # --- media files of this archive (the shared store)
    def check_media(self) -> None:
        db = self.db
        a = self.arch
        rows = [dict(r) for r in db.execute(
            "SELECT b.sha256, b.size, b.path, b.checked_at FROM store.blob b WHERE b.sha256 IN "
            "(SELECT r.sha256 FROM store.media_ref r WHERE r.status='done' AND r.key IN (SELECT key FROM main.media_use))")]
        bad: list[tuple[dict, str]] = []
        ok: list[dict] = []
        for i, b in enumerate(rows):
            if i % 2000 == 0:
                self.cancel()
                self.step("media", i, len(rows))
            self.counts["media"] += 1
            try:
                size = (a.media / b["path"]).stat().st_size
            except OSError:
                bad.append((b, "missing"))
                continue
            if b["size"] is not None and size != b["size"]:
                bad.append((b, "damaged"))
            else:
                ok.append(b)
        if self.full:
            todo = ok
        else:
            total = sum(b["size"] or 0 for b in ok)
            budget = max(total // MEDIA_SHARE, MEDIA_MIN)
            todo, acc = [], 0
            for b in sorted(ok, key=lambda b: (b["checked_at"] or 0, b["sha256"])):
                if acc >= budget:
                    break
                todo.append(b)
                acc += b["size"] or 0
        now = now_ts()
        for i in range(0, len(todo), 500):   # marked before hashing: a parallel sync takes other files
            part = todo[i:i + 500]
            db.executemany("UPDATE store.blob SET checked_at=? WHERE sha256=?", [(now, b["sha256"]) for b in part])
        db.commit()
        nbytes = sum(b["size"] or 0 for b in todo)
        done_bytes = 0
        last = time.monotonic()
        for i, b in enumerate(todo):
            self.cancel()
            if time.monotonic() - last > 0.5:
                self.step("media-hash", done_bytes, nbytes)
                last = time.monotonic()
            try:
                sha = file_sha256(a.media / b["path"])
            except OSError:
                bad.append((b, "missing"))
                continue
            self.counts["media_hashed"] += 1
            self.counts["media_bytes"] += b["size"] or 0
            done_bytes += b["size"] or 0
            if sha != b["sha256"]:
                bad.append((b, "damaged"))
        self.step("media-hash", nbytes, nbytes)
        for b, kind in bad:
            quarantine_media(a.library, db, b["sha256"], b["path"])
            self.add(finding("media", b["path"], kind, "pending"))
        db.commit()


def quarantine_dir(arch: Archive) -> Path:
    return arch.state_dir / "quarantine" / time.strftime("%Y%m%d-%H%M%S")


def set_aside(arch: Archive, path: Path, qdir: Path | None = None) -> None:
    """Move a damaged file of the archive to .state/quarantine/<time>/ (same relative path): never deleted."""
    if not path.exists():
        return
    qdir = qdir or quarantine_dir(arch)
    try:
        dest = qdir / path.resolve().relative_to(arch.root)
    except ValueError:
        dest = qdir / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, dest)
    log.warning(f"[целостность] повреждённый файл перенесён: {dest}")


def quick_check(db: sqlite3.Connection, schema: str = "main") -> None:
    """PRAGMA quick_check (structure, not blob contents): DbDamaged when the database file is broken."""
    try:
        res = [r[0] for r in db.execute(f"PRAGMA {schema}.quick_check(5)")]
    except sqlite3.DatabaseError as e:
        raise DbDamaged(str(e), schema) from e
    if res != ["ok"]:
        raise DbDamaged("; ".join(map(str, res))[:300], schema)


def quarantine_media(library: Path, db: sqlite3.Connection, sha: str, path: str) -> None:
    """A damaged or missing media file: moved aside, its catalog row dropped, every key that pointed to it waits for
    a new download that is compared with `sha` (stage_media). Safe to repeat (two archives may find it at once)."""
    src = library / "media" / path
    dest = library / ".state" / "quarantine" / "media" / time.strftime("%Y%m%d") / Path(path).name
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            replace_file(src, dest)
        except FileNotFoundError:
            pass
    db.execute("DELETE FROM store.blob WHERE sha256=?", (sha,))
    db.execute("UPDATE store.media_ref SET status='pending', sha256=NULL, via=NULL, attempts=0, expect_sha=? "
               "WHERE sha256=?", (sha, sha))


def unslim(c: dict, users: dict, built: int = 0) -> dict:
    """A comment of the pages (viewdb.slim_comment) in the shape of DTF's answer, as far as it goes."""
    uid = c.get("author")
    u = users.get(uid) or {}
    author: dict[str, Any] = {"id": uid, "name": u.get("name"), "nickname": u.get("nickname"), "uri": u.get("uri")}
    if u.get("avatar"):
        author["avatar"] = {"type": "image", "data": {"uuid": u["avatar"]}}
    out: dict[str, Any] = {
        "id": c["id"], "date": c.get("date"), "author": author, "replyTo": c.get("replyTo") or 0,
        "level": c.get("level") or 0, "text": c.get("text") or "", "media": c.get("media") or [],
        "isRemoved": bool(c.get("isRemoved")), "isEdited": bool(c.get("isEdited")),
        "isRemovedByModerator": bool(c.get("removedByModerator")), "isHiddenByBan": bool(c.get("hiddenByBan")),
        "replyCount": c.get("replyCount") or 0, "entry": c.get("entry"),
        "likes": {"counterLikes": c.get("likes") or 0}, "_restored": "view"}
    if c.get("site"):
        out["_site"] = {"state": c["site"], "at": built or now_ts()}
    return out


def export_my_comments(arch: Archive, years: Iterable[str]) -> int:
    """Write raw/my-comments/<year> from the database (comments no copy could restore are left out until the sync
    downloads them again) and record the files."""
    db = arch.db
    years = set(years)
    by_year: dict[str, list[dict]] = {y: [] for y in years}
    for row in db.execute("SELECT date, raw FROM my_comments ORDER BY date, id"):
        y = ts_year(row["date"] or 0)
        if y in by_year:
            c = safe_unpack(row["raw"])
            if c and not c.get("_damaged"):
                by_year[y].append(c)
    n = 0
    for y, rows in by_year.items():
        p = arch.raw_my_comments(y)
        note(db, arch, p, write_jsonl_gz(p, rows))
        n += len(rows)
    arch.set_meta(META_EXPORT_DIRTY, None)
    db.commit()
    return n


# ---------------------------------------------------------------------- repairs that need DTF
def queue(db: sqlite3.Connection, kind: str, item_id: int, expect_sig: str | None, expect_date: int | None,
          reason: str) -> None:
    db.execute("INSERT INTO repairs(kind, item_id, expect_sig, expect_date, reason, found_at, attempts) "
               "VALUES (?,?,?,?,?,?,0) ON CONFLICT(kind, item_id) DO UPDATE SET reason=excluded.reason",
               (kind, item_id, expect_sig, expect_date, reason, now_ts()))


def pending(db: sqlite3.Connection, kind: str) -> dict[int, sqlite3.Row]:
    return {r["item_id"]: r for r in db.execute("SELECT * FROM repairs WHERE kind=?", (kind,))}


def settle(arch: Archive, kind: str, item_id: int, fresh: dict | None, entry_id: int | None = None,
           title: str = "") -> str | None:
    """A damaged item came again from DTF (fresh; None: DTF doesn't have it): compare it with the version the archive
    had. Returns the outcome, None when the item wasn't waiting for a repair."""
    db = arch.db
    r = db.execute("SELECT * FROM repairs WHERE kind=? AND item_id=?", (kind, item_id)).fetchone()
    if r is None:
        return None
    hk = "post" if kind == "post" else "comment"
    from .history import is_stub
    if fresh is None or is_stub(hk, fresh):
        outcome = "lost"
        record_damaged(db, hk, item_id, r["expect_sig"], r["expect_date"], "lost", entry_id)
    elif r["expect_sig"] and content_sig(hk, fresh) == r["expect_sig"]:
        outcome = "exact"
    else:
        outcome = "replaced"
        record_damaged(db, hk, item_id, r["expect_sig"], r["expect_date"], "replaced", entry_id)
    db.execute("DELETE FROM repairs WHERE kind=? AND item_id=?", (kind, item_id))
    text = title or (fresh or {}).get("title") or (fresh or {}).get("text") or ""
    log_findings(arch, [finding(hk, item_id, r["reason"] or "damaged", outcome, short(text, 120))])
    log.info(f"[целостность] {hk} {item_id}: {OUTCOMES[outcome]}")
    return outcome


def settle_tree(arch: Archive, kind: str, xid: int, events: list, entry_id: int | None = None) -> bool:
    """A damaged comment tree or thread came again from DTF and was merged with the best surviving copy. Comments
    whose text differs from that copy keep it as a version (the sync records the edit); since that copy may be older
    than what the archive had, each of them also gets a `damaged` marker."""
    db = arch.db
    r = db.execute("SELECT * FROM repairs WHERE kind=? AND item_id=?", (kind, xid)).fetchone()
    if r is None:
        return False
    n = 0
    for ev in events:
        if ev[0] == "edit":
            c = ev[1]
            n += record_damaged(db, "comment", c["id"], content_sig("comment", c), None, "replaced",
                                entry_id or (c.get("entry") or {}).get("id"))
    db.execute("DELETE FROM repairs WHERE kind=? AND item_id=?", (kind, xid))
    what = "комментарии под постом" if kind == "tree" else "ветка обсуждения"
    log_findings(arch, [finding(kind, xid, r["reason"] or "damaged", "reloaded", what, changed=n)])
    return True


def attempt_failed(db: sqlite3.Connection, kind: str, item_id: int) -> None:
    db.execute("UPDATE repairs SET attempts=attempts+1 WHERE kind=? AND item_id=?", (kind, item_id))


def finish(arch: Archive, ran: Iterable[str], checked: bool = True) -> None:
    """End of a sync that went fine: repairs that kept failing end as lost, the mirror takes the new files, the
    databases are copied (once a day, only after a check: never a copy of something damaged)."""
    db = arch.db
    kinds = set()
    ran = set(ran)
    if "posts" in ran:
        kinds |= {"post", "tree"}
    if "comments" in ran:
        kinds.add("comment")
    if "threads" in ran:
        kinds.add("thread")
    for r in db.execute("SELECT * FROM repairs WHERE attempts >= ?", (MAX_ATTEMPTS,)).fetchall():
        if r["kind"] not in kinds:
            continue
        if r["kind"] in ("post", "comment"):
            settle(arch, r["kind"], r["item_id"], None, r["item_id"] if r["kind"] == "post" else None)
        else:
            db.execute("DELETE FROM repairs WHERE kind=? AND item_id=?", (r["kind"], r["item_id"]))
            log_findings(arch, [finding(r["kind"], r["item_id"], r["reason"] or "damaged", "partial")])
    db.commit()
    refresh_mirror(arch)
    if checked and db.execute("SELECT 1 FROM repairs LIMIT 1").fetchone() is None:
        try:
            backup_dbs(arch)
        except (OSError, sqlite3.Error) as e:
            log.warning(f"[целостность] копия базы не сделана: {e}")


def refresh_mirror(arch: Archive) -> int:
    """Copy into .state/backup/raw/ the raw files that changed since the last time (verified against the manifest
    while copying: a file damaged meanwhile is left for the next check)."""
    db = arch.db
    root = mirror_root(arch)
    n = 0
    rows = db.execute("SELECT path, sha256 FROM files WHERE backup_sha IS NULL OR backup_sha != sha256").fetchall()
    for r in rows:
        src = arch.raw / r["path"]
        try:
            data = src.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() != r["sha256"]:
            continue
        dest = root / r["path"]
        try:
            atomic_write_bytes(dest, data)
        except OSError as e:
            log.warning(f"[целостность] копия {r['path']} не записана: {e}")
            continue
        db.execute("UPDATE files SET backup_sha=? WHERE path=?", (r["sha256"], r["path"]))
        n += 1
    db.commit()
    # copies of files the archive no longer has (comments dropped, a year moved)
    if root.is_dir():
        known = {r[0] for r in db.execute("SELECT path FROM files")}
        for d in list(RAW_DIRS) + [""]:
            base = root / d if d else root
            if not base.is_dir():
                continue
            for p in base.iterdir():
                if p.is_file() and (f"{d}/{p.name}" if d else p.name) not in known and p.name.endswith(".gz"):
                    p.unlink(missing_ok=True)
    if n:
        log.debug(f"[целостность] копия raw обновлена: {n} файлов")
    return n


def manifest_clean(arch: Archive) -> bool:
    """Every raw file matches its manifest row (cheap: hashes only) — what render checks before building pages."""
    db = arch.db
    if arch.get_meta(META_BASELINE) is None:
        return False
    rows = {r[0]: r[1] for r in db.execute("SELECT path, sha256 FROM files")}
    for name in RAW_FILES:
        if (arch.raw / name).exists() and name not in rows:
            return False
    for d, ext in RAW_DIRS.items():
        if (arch.raw / d).is_dir():
            for p in (arch.raw / d).iterdir():
                if p.is_file() and p.name.endswith(ext) and f"{d}/{p.name}" not in rows:
                    return False
    paths = {arch.raw / r: sha for r, sha in rows.items()}
    got = hash_files(list(paths))
    return all(got.get(p, (None, 0))[0] == sha for p, sha in paths.items())


def hash_files(paths: list[Path], cancel: Callable[[], None] | None = None,
               progress: Callable[[int, int], None] | None = None) -> dict[Path, tuple[str | None, int]]:
    """{path: (sha256 or None when unreadable, size)} — in a few threads."""
    from concurrent.futures import ThreadPoolExecutor

    def one(p: Path) -> tuple[str | None, int]:
        try:
            data = p.read_bytes()
        except OSError:
            return None, 0
        return hashlib.sha256(data).hexdigest(), len(data)
    out: dict[Path, tuple[str | None, int]] = {}
    with ThreadPoolExecutor(max_workers=HASH_THREADS, thread_name_prefix=_thread_prefix() + "hash") as pool:
        for i, (p, res) in enumerate(zip(paths, pool.map(one, paths, chunksize=64))):
            out[p] = res
            if i % 500 == 0:
                if cancel:
                    cancel()
                if progress:
                    progress(i, len(paths))
    return out


def _thread_prefix() -> str:
    """Worker threads keep the job's name prefix: the app routes their log lines to the job (web/jobs.py)."""
    import threading
    name = threading.current_thread().name
    return name.split("-", 1)[0] + "-" if name.startswith("job") else ""


def render_gate(arch: Archive) -> None:
    """Pages are built only from raw/ that passed the check: otherwise a damaged file would drop out of the pages
    (or break the build) and the pages' copy — a source of repairs — would be lost. Repairs what it can locally."""
    if not arch.exists() or manifest_clean(arch):
        return
    from .sync import acquire_lock, release_lock
    other = acquire_lock(arch.lock_path)
    if other:
        raise SystemExit("Сборка страниц отложена: с архивом работает синхронизация, она сама проверит файлы.")
    try:
        log.info("[целостность] перед сборкой страниц: файлы архива отличаются от описи — проверка")
        Check(arch, mode="quick", media=False).run()
    finally:
        release_lock(arch.lock_path)


# ---------------------------------------------------------------------- databases: copies and restoring
DB_BACKUP_EVERY = 86400


def db_backup_path(arch: Archive) -> Path:
    return arch.state_dir / "backup" / "state.sqlite"


def store_backup_path(library: Path) -> Path:
    return library / ".state" / "backup" / "media.sqlite"


def _vacuum_into(db: sqlite3.Connection, schema: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    db.commit()
    db.execute(f"VACUUM {schema} INTO ?", (str(tmp),))
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    replace_file(tmp, dest)


def backup_dbs(arch: Archive, force: bool = False) -> bool:
    """A consistent copy of state.sqlite (and of the shared media.sqlite) at most once a day: what a database that
    breaks is restored from (recover_db)."""
    now = now_ts()
    did = False
    if force or now - int(arch.get_meta(META_DB_BACKUP) or 0) >= DB_BACKUP_EVERY:
        _vacuum_into(arch.db, "main", db_backup_path(arch))
        arch.set_meta(META_DB_BACKUP, now)
        arch.commit()
        did = True
    sb = store_backup_path(arch.library)
    try:
        fresh = now - sb.stat().st_mtime < DB_BACKUP_EVERY
    except OSError:
        fresh = False
    if force or not fresh:
        from .sync import acquire_lock, release_lock
        lock = arch.library / ".state" / "store-backup.lock"
        if not acquire_lock(lock):   # another archive's sync is copying it right now
            try:
                _vacuum_into(arch.db, "store", sb)
                did = True
            finally:
                release_lock(lock)
    if did:
        log.info("[целостность] копия базы архива обновлена")
    return did


def backup_history_body(arch: Archive, row: Any) -> bytes | None:
    """The body of a kept version from the database copy, if it is the same version."""
    b = db_backup_path(arch)
    if not b.exists():
        return None
    try:
        con = sqlite3.connect(f"{b.resolve().as_uri()}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT body FROM history WHERE id=?", (row["id"],)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    obj = safe_unpack(r[0]) if r else None
    if obj is None or (row["sig"] and content_sig(row["kind"], obj) != row["sig"]):
        return None
    return r[0]


# rows taken from a damaged database (newer than its copy) only when they still read whole
def _valid_blob(col: str) -> Callable[[dict], bool]:
    return lambda r: r.get(col) is None or safe_unpack(r[col]) is not None


SALVAGE_MAIN = (("meta", "REPLACE", None), ("posts", "REPLACE", None), ("my_comments", "REPLACE", _valid_blob("raw")),
                ("slices", "REPLACE", None), ("threads", "REPLACE", None), ("media_use", "IGNORE", None),
                ("history", "IGNORE", _valid_blob("body")), ("files", "REPLACE", None), ("repairs", "REPLACE", None))
SALVAGE_STORE = (("media_ref", "REPLACE", None), ("blob", "REPLACE", None))


def salvage(src: Path, dst: sqlite3.Connection, tables: Iterable[tuple], schema: str = "main") -> Counter:
    """Copy what still reads of a damaged database into `dst` (row by row: a broken page ends that table)."""
    n: Counter = Counter()
    try:
        con = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return n
    con.row_factory = sqlite3.Row
    try:
        for table, mode, ok in tables:
            try:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                have = {r[1] for r in dst.execute(f"PRAGMA {schema}.table_info({table})")}
                cols = [c for c in cols if c in have]
                if not cols:
                    continue
                cur = con.execute(f"SELECT {', '.join(cols)} FROM {table}")
            except sqlite3.Error:
                continue
            sql = (f"INSERT OR {mode} INTO {schema}.{table}({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))})")
            while True:
                try:
                    row = cur.fetchone()
                except sqlite3.Error:
                    break
                if row is None:
                    break
                d = dict(row)
                if ok and not ok(d):
                    continue
                try:
                    dst.execute(sql, [d[c] for c in cols])
                    n[table] += 1
                except sqlite3.Error:
                    continue
        dst.commit()
    finally:
        con.close()
    return n


def _set_aside_db(path: Path, qdir: Path) -> Path:
    qdir.mkdir(parents=True, exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        f = Path(str(path) + suf)
        if f.exists():
            try:
                replace_file(f, qdir / f.name)
            except OSError as e:
                raise DbDamaged(f"файл базы занят другой программой ({e}) — восстановлю при следующей попытке") from e
    return qdir / path.name


def recover_db(arch: Archive) -> dict:
    """state.sqlite failed quick_check: it goes to quarantine, its daily copy takes its place and whatever still reads
    of the damaged one (newer rows) is copied over; the check that follows repairs the rest."""
    from .state import SCHEMA, _add_columns
    path = arch.db_path
    backup = db_backup_path(arch)
    arch.close()
    qdir = quarantine_dir(arch)
    damaged = _set_aside_db(path, qdir)
    tmp = path.with_name(path.name + ".restore")
    tmp.unlink(missing_ok=True)
    if backup.exists():
        import shutil
        shutil.copyfile(backup, tmp)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        _add_columns(con)
        n = salvage(damaged, con, SALVAGE_MAIN)
        quick_check(con)
    finally:
        con.close()
    replace_file(tmp, path)
    when = arch.get_meta(META_DB_BACKUP) if backup.exists() else None
    info = {"backup": bool(backup.exists()), "backup_at": when, "rows": dict(n)}
    log_findings(arch, [finding("database", "state.sqlite", "damaged", "restored",
                                "база архива", source="копия базы" if backup.exists() else "уцелевшие строки")])
    arch.commit()
    log.warning(f"[целостность] база архива была повреждена: восстановлена из "
                f"{'копии и ' if backup.exists() else ''}уцелевших строк ({dict(n)}); повреждённая — в {qdir}")
    return info


def recover_store(arch: Archive) -> dict:
    """The shared media.sqlite failed quick_check: restored like recover_db while no other archive syncs (their
    locks are held; this archive's own lock is held by the caller)."""
    from .state import STORE_COLUMNS, STORE_SCHEMA, archive_dirs, store_path
    from .sync import acquire_lock, release_lock
    library = arch.library
    held: list[Path] = []
    try:
        for d in archive_dirs(library):
            lock = Archive(d, library).lock_path
            if lock == arch.lock_path:
                continue
            if acquire_lock(lock):
                raise DbDamaged("общая база медиа повреждена, а другой архив сейчас синхронизируется — "
                                "восстановлю при следующей синхронизации", "store")
            held.append(lock)
        path = store_path(library)
        backup = store_backup_path(library)
        arch.close()
        qdir = library / ".state" / "quarantine" / time.strftime("%Y%m%d-%H%M%S")
        damaged = _set_aside_db(path, qdir)
        tmp = path.with_name(path.name + ".restore")
        tmp.unlink(missing_ok=True)
        if backup.exists():
            import shutil
            shutil.copyfile(backup, tmp)
        con = sqlite3.connect(":memory:")
        try:
            con.execute("ATTACH DATABASE ? AS store", (str(tmp),))
            con.executescript(STORE_SCHEMA)
            for table, added in STORE_COLUMNS.items():
                cols = {r[1] for r in con.execute(f"PRAGMA store.table_info({table})")}
                for col, typ in added:
                    if col not in cols:
                        con.execute(f"ALTER TABLE store.{table} ADD COLUMN {col} {typ}")
            n = salvage(damaged, con, SALVAGE_STORE, "store")
            quick_check(con, "store")
        finally:
            con.close()
        replace_file(tmp, path)
        log_findings(arch, [finding("database", "media.sqlite", "damaged", "restored", "общая база медиа",
                                    source="копия базы" if backup.exists() else "уцелевшие строки")])
        arch.commit()
        log.warning(f"[целостность] общая база медиа была повреждена: восстановлена ({dict(n)}); повреждённая — в {qdir}")
        return {"backup": backup.exists(), "rows": dict(n)}
    finally:
        for lock in held:
            release_lock(lock)
