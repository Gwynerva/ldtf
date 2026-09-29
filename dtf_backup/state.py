"""Archive layout and SQLite state (progress, cursors, queues) + the shared media store.

Layout of a library (the `archive/` folder):
  archive/media/<sha[:2]>/<sha256>.<ext>   media shared by all archives (content-addressed)
  archive/.state/media.sqlite              shared media catalog: media_ref (key -> sha256, status), blob
  archive/<nick>/                          one archive per DTF user (raw/, data/, md/, .state/state.sqlite ...)

Each archive's state.sqlite ATTACHes the shared catalog as `store`; `media_use` (which keys this
archive needs) stays per archive. The database is only touched from one thread per process; worker
threads do network and file IO and hand results back, so every unit of work is committed at once.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION
from .util import log, now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    date INTEGER, date_modified INTEGER, comments_count INTEGER,
    is_repost INTEGER DEFAULT 0,
    listed_at INTEGER,
    content_modified INTEGER, content_fetched_at INTEGER, content_status TEXT, content_error TEXT,
    tree_count INTEGER, tree_fetched_at INTEGER, tree_status TEXT, tree_error TEXT,
    site_state TEXT, site_state_at INTEGER          -- what happened on DTF (guard.py): removed | wiped | gone ...
);

CREATE TABLE IF NOT EXISTS my_comments (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER, date INTEGER, level INTEGER, reply_to INTEGER, reply_count INTEGER,
    thread_id TEXT, last_mod INTEGER, is_removed INTEGER,
    raw BLOB, seen_at INTEGER,
    site_state TEXT, site_state_at INTEGER          -- set when DTF lost it; raw keeps the archived version
);
CREATE INDEX IF NOT EXISTS my_comments_entry ON my_comments(entry_id);
CREATE INDEX IF NOT EXISTS my_comments_date ON my_comments(date);

CREATE TABLE IF NOT EXISTS slices (
    start INTEGER PRIMARY KEY, end INTEGER,
    last_id INTEGER, last_sv INTEGER, pages INTEGER DEFAULT 0, done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS threads (
    entry_id INTEGER PRIMARY KEY,
    fetched_at INTEGER, max_my_comment_id INTEGER, mode TEXT, n_items INTEGER, n_kept INTEGER,
    missing INTEGER, status TEXT, error TEXT, attempts INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media_use (key TEXT, owner TEXT, PRIMARY KEY (key, owner)) WITHOUT ROWID;
"""

STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS store.media_ref (
    key TEXT PRIMARY KEY,             -- uuid, or URL for non-uuid media (favicons, scaled avatars, raw reactions)
    sig TEXT,                         -- metadata signature for dedup probing (NULL if unknown)
    kind TEXT,                        -- metadata 'type' (jpg/png/gif/...)
    sha256 TEXT, status TEXT DEFAULT 'pending',   -- pending | done | missing | error
    via TEXT, error TEXT, attempts INTEGER DEFAULT 0, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS store.media_ref_status ON media_ref(status);
CREATE TABLE IF NOT EXISTS store.blob (
    sha256 TEXT PRIMARY KEY, size INTEGER, ext TEXT, mime TEXT, sig TEXT, path TEXT
);
CREATE INDEX IF NOT EXISTS store.blob_sig ON blob(sig);
"""


# meta keys read outside the sync (scheduler, app pages, tray)
META_GUARD = "guard"                 # the archive guard stopped the sync: {kind, message, details, ts} (guard.py)
META_LAST_SYNC = "last_sync"         # the last finished sync: {ts, finished, stages, stats}
META_SYNC_ATTEMPT = "sync_attempt"   # the last try for the scheduler's backoff: {ts, ok, fails}


def pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack(b: bytes) -> Any:
    return json.loads(zlib.decompress(b).decode("utf-8"))


def store_path(library: Path) -> Path:
    return library / ".state" / "media.sqlite"


def state_db(root: Path) -> Path:
    """An archive's own database."""
    return root / ".state" / "state.sqlite"


def connect_ro(path: Path, timeout: float = 5) -> sqlite3.Connection:
    """Read-only connection that never creates or migrates anything (the path is URI-escaped: '#', '%', spaces)."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _attach_store(db: sqlite3.Connection, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db.execute("ATTACH DATABASE ? AS store", (str(path),))
    db.execute("PRAGMA store.journal_mode=WAL")
    db.executescript(STORE_SCHEMA)


def open_store(library: Path) -> sqlite3.Connection:
    """Standalone connection to the shared media catalog (tables available as store.*)."""
    db = sqlite3.connect(":memory:", timeout=60)
    db.row_factory = sqlite3.Row
    _attach_store(db, store_path(library))
    return db


def is_archive_dir(p: Path) -> bool:
    return p.is_dir() and not p.name.startswith(".") and p.name != "media" and (
        state_db(p).exists() or (p / "raw" / "profile.json.gz").exists())


def archive_dirs(library: Path) -> list[Path]:
    if not library.exists():
        return []
    return sorted((p for p in library.iterdir() if is_archive_dir(p)), key=lambda p: p.name.lower())


class Archive:
    def __init__(self, root: Path, library: Path | None = None):
        self.root = root.resolve()
        self.library = (library or self.root.parent).resolve()
        self.nick = self.root.name
        self.state_dir = self.root / ".state"
        self.raw = self.root / "raw"
        self.media = self.library / "media"            # shared
        self.store_path = store_path(self.library)
        self.db_path = state_db(self.root)
        self.view_path = self.state_dir / "view.sqlite"
        self.lock_path = self.state_dir / "sync.lock"
        self.settings_path = self.state_dir / "settings.json"
        self.log_path = self.state_dir / "sync.log"
        self.report_path = self.state_dir / "render-report.json"
        self._db: sqlite3.Connection | None = None

    # raw file locations
    def raw_profile(self) -> Path: return self.raw / "profile.json.gz"
    def raw_assets(self) -> Path: return self.raw / "assets.json.gz"
    def raw_post(self, pid: int) -> Path: return self.raw / "posts" / f"{pid}.json.gz"
    def raw_post_tree(self, pid: int) -> Path: return self.raw / "post-trees" / f"{pid}.json.gz"
    def raw_thread(self, eid: int) -> Path: return self.raw / "threads" / f"{eid}.json.gz"
    def raw_post_history(self, pid: int, ver: int) -> Path: return self.raw / "history" / "posts" / str(pid) / f"{ver}.json.gz"
    def raw_my_comments(self, year: str) -> Path: return self.raw / "my-comments" / f"{year}.jsonl.gz"

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.db_path, timeout=60)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            _add_columns(db)
            _attach_store(db, self.store_path)
            self._db = db
            if self.get_meta("schema_version") is None:
                self.set_meta("schema_version", SCHEMA_VERSION)
                db.commit()
            self._migrate_legacy_media()
        return self._db

    def exists(self) -> bool:
        return self.db_path.exists()

    def close(self) -> None:
        if self._db is not None:
            self._db.commit()
            self._db.close()
            self._db = None

    # meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM main.meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO main.meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def commit(self) -> None:
        self.db.commit()

    # settings (edited from the app)
    def settings(self) -> dict:
        from .settings import load_settings
        return load_settings(self.settings_path)

    def media_usage(self) -> tuple[int, int]:
        """(files, bytes) of the shared store this archive uses; (0, 0) when its database can't be read."""
        try:
            r = self.db.execute("SELECT COUNT(*), COALESCE(SUM(b.size),0) FROM store.blob b WHERE b.sha256 IN "
                                "(SELECT r.sha256 FROM store.media_ref r WHERE r.key IN (SELECT key FROM main.media_use))"
                                ).fetchone()
            return r[0], r[1]
        except sqlite3.Error as e:
            log.warning(f"[{self.nick}] размер медиа архива не посчитан: {e}")
            return 0, 0

    # media queue
    def queue_media(self, refs: Iterable[tuple[str, str | None, str | None]], owner: str) -> int:
        """refs: (key, sig, kind). Returns the number of keys new to the shared store."""
        n = 0
        ts = now_ts()
        for key, sig, kind in refs:
            cur = self.db.execute(
                "INSERT INTO store.media_ref(key,sig,kind,status,updated_at) VALUES(?,?,?,'pending',?) "
                "ON CONFLICT(key) DO NOTHING", (key, sig, kind, ts))
            n += cur.rowcount
            self.db.execute("INSERT OR IGNORE INTO main.media_use(key,owner) VALUES(?,?)", (key, owner))
        return n

    # ------------------------------------------------------------------ legacy layout
    def _migrate_legacy_media(self) -> None:
        """Before the shared store each archive had media/ + media_ref/blob tables of its own."""
        db = self._db
        assert db is not None
        tables = {r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
        legacy_dir = self.root / "media"
        if "media_ref" not in tables and not legacy_dir.exists():
            return
        log.info(f"[{self.nick}] перенос медиа в общее хранилище {self.media} …")
        if "blob" in tables:
            db.execute("INSERT OR IGNORE INTO store.blob(sha256,size,ext,mime,sig,path) "
                       "SELECT sha256,size,ext,mime,sig,path FROM main.blob")
        if "media_ref" in tables:
            db.execute("INSERT OR IGNORE INTO store.media_ref(key,sig,kind,sha256,status,via,error,attempts,updated_at) "
                       "SELECT key,sig,kind,sha256,status,via,error,attempts,updated_at FROM main.media_ref")
            # a key already known to the store but not yet downloaded there: take the finished legacy result
            db.execute("UPDATE store.media_ref SET sha256=l.sha256, status=l.status, via=l.via, error=NULL "
                       "FROM main.media_ref AS l WHERE l.key=store.media_ref.key AND l.status='done' "
                       "AND store.media_ref.status!='done'")
        db.commit()
        if legacy_dir.exists():
            moved = 0
            for shard in legacy_dir.iterdir():
                if not (shard.is_dir() and len(shard.name) == 2):
                    continue
                dest = self.media / shard.name
                dest.mkdir(parents=True, exist_ok=True)
                for f in shard.iterdir():
                    target = dest / f.name
                    if target.exists():
                        f.unlink()
                    else:
                        os.replace(f, target)
                        moved += 1
            shutil.rmtree(legacy_dir, ignore_errors=True)
            log.info(f"[{self.nick}] перенесено файлов: {moved}")
        for t in ("media_ref", "blob"):
            if t in tables:
                db.execute(f"DROP TABLE main.{t}")
        db.commit()


def read_meta(root: Path, keys: Iterable[str]) -> dict[str, Any] | None:
    """Meta values of an archive, read-only and cheap (no schema setup, no store): {key: value} for the keys present;
    {} for an archive without state yet, None if its database can't be read (callers must not guess then)."""
    p = state_db(root)
    if not p.exists():
        return {}
    keys = list(keys)
    try:
        con = connect_ro(p)
        try:
            rows = con.execute(f"SELECT key, value FROM meta WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
        finally:
            con.close()
        return {r[0]: json.loads(r[1]) for r in rows}
    except (sqlite3.Error, ValueError) as e:
        log.debug(f"[{root.name}] meta не читается: {e}")
        return None


def peek_meta(root: Path, key: str) -> Any:
    """One meta value (None if absent or unreadable): for page chrome and the tray."""
    return (read_meta(root, [key]) or {}).get(key)


def _add_columns(db: sqlite3.Connection) -> None:
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS keeps old tables as they are)."""
    for table in ("posts", "my_comments"):
        cols = {r[1] for r in db.execute(f"PRAGMA main.table_info({table})")}
        for col, typ in (("site_state", "TEXT"), ("site_state_at", "INTEGER")):
            if col not in cols:
                db.execute(f"ALTER TABLE main.{table} ADD COLUMN {col} {typ}")
    db.commit()


def gc_pending(library: Path) -> Path:
    """Marker of a postponed media cleanup (the app retries it on its next start)."""
    return library / ".state" / "gc-pending"


def gc_media(library: Path, busy: bool = False) -> dict[str, Any]:
    """Delete blobs (and catalog rows) no longer used by any archive.

    Never guesses: it holds every archive's sync lock while it runs (a sync that starts meanwhile waits for the next
    try), and if a sync is running (`busy`: in this app; a held lock: in another process) or any archive's database
    can't be read, nothing is deleted and the cleanup is postponed.
    Returns {"status": "done"|"postponed", "files", "bytes", "reason"}."""
    from .sync import acquire_lock, release_lock   # sync imports this module
    marker = gc_pending(library)

    def postpone(reason: str) -> dict[str, Any]:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(reason, encoding="utf-8")
        log.warning(f"[медиа] очистка хранилища отложена: {reason}")
        return {"status": "postponed", "files": 0, "bytes": 0, "reason": reason}

    if busy:
        return postpone("идёт синхронизация")
    dirs = [d for d in archive_dirs(library) if state_db(d).exists()]
    held: list[Path] = []
    try:
        for d in dirs:
            lock = Archive(d, library).lock_path
            if acquire_lock(lock):
                return postpone("идёт синхронизация")
            held.append(lock)
        files = size = 0
        if store_path(library).exists():
            used: set[str] = set()
            for d in dirs:
                arch = Archive(d, library)   # opening migrates a legacy layout, so media_use exists
                try:
                    used.update(r[0] for r in arch.db.execute("SELECT DISTINCT key FROM main.media_use"))
                except sqlite3.Error as e:
                    return postpone(f"не читается архив {d.name}: {e}")
                finally:
                    arch.close()
            db = open_store(library)
            try:
                keep = {r[1] for r in db.execute("SELECT key, sha256 FROM store.media_ref WHERE sha256 IS NOT NULL")
                        if r[0] in used}
                for row in db.execute("SELECT sha256, path, size FROM store.blob").fetchall():
                    if row["sha256"] in keep:
                        continue
                    try:
                        (library / "media" / row["path"]).unlink()
                        files += 1
                        size += row["size"] or 0
                    except FileNotFoundError:
                        pass
                    db.execute("DELETE FROM store.blob WHERE sha256=?", (row["sha256"],))
                stale = [r[0] for r in db.execute("SELECT key FROM store.media_ref") if r[0] not in used]
                db.executemany("DELETE FROM store.media_ref WHERE key=?", [(k,) for k in stale])
                db.commit()
            finally:
                db.close()
    finally:
        for lock in held:
            release_lock(lock)
    marker.unlink(missing_ok=True)
    return {"status": "done", "files": files, "bytes": size, "reason": ""}
