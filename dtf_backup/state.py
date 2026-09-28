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


def pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack(b: bytes) -> Any:
    return json.loads(zlib.decompress(b).decode("utf-8"))


def store_path(library: Path) -> Path:
    return library / ".state" / "media.sqlite"


def open_store(library: Path) -> sqlite3.Connection:
    """Standalone connection to the shared media catalog (tables available as store.*)."""
    p = store_path(library)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(":memory:", timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ? AS store", (str(p),))
    db.execute("PRAGMA store.journal_mode=WAL")
    db.executescript(STORE_SCHEMA)
    return db


def is_archive_dir(p: Path) -> bool:
    return p.is_dir() and not p.name.startswith(".") and p.name != "media" and (
        (p / ".state" / "state.sqlite").exists() or (p / "raw" / "profile.json.gz").exists())


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
        self.data = self.root / "data"
        self.md = self.root / "md"
        self.db_path = self.state_dir / "state.sqlite"
        self.view_path = self.state_dir / "view.sqlite"
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
            db.execute("ATTACH DATABASE ? AS store", (str(self.store_path),))
            db.execute("PRAGMA store.journal_mode=WAL")
            db.executescript(STORE_SCHEMA)
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


def peek_meta(root: Path, key: str) -> Any:
    """One meta value of an archive, read-only and cheap (no schema setup): for page chrome and the tray."""
    p = root / ".state" / "state.sqlite"
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _add_columns(db: sqlite3.Connection) -> None:
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS keeps old tables as they are)."""
    for table in ("posts", "my_comments"):
        cols = {r[1] for r in db.execute(f"PRAGMA main.table_info({table})")}
        for col, typ in (("site_state", "TEXT"), ("site_state_at", "INTEGER")):
            if col not in cols:
                db.execute(f"ALTER TABLE main.{table} ADD COLUMN {col} {typ}")
    db.commit()


def gc_media(library: Path) -> tuple[int, int]:
    """Delete blobs (and catalog rows) no longer used by any archive. Returns (files, bytes)."""
    used: set[str] = set()
    for d in archive_dirs(library):
        p = d / ".state" / "state.sqlite"
        if not p.exists():
            continue
        con = sqlite3.connect(p, timeout=60)
        try:
            used.update(r[0] for r in con.execute("SELECT DISTINCT key FROM media_use"))
        except sqlite3.OperationalError:
            pass
        con.close()
    db = open_store(library)
    keep = {r[1] for r in db.execute("SELECT key, sha256 FROM store.media_ref WHERE sha256 IS NOT NULL")
            if r[0] in used}
    files = size = 0
    for row in db.execute("SELECT sha256, path, size FROM store.blob").fetchall():
        if row["sha256"] in keep:
            continue
        f = library / "media" / row["path"]
        try:
            f.unlink()
            files += 1
            size += row["size"] or 0
        except FileNotFoundError:
            pass
        db.execute("DELETE FROM store.blob WHERE sha256=?", (row["sha256"],))
    stale = [r[0] for r in db.execute("SELECT key FROM store.media_ref") if r[0] not in used]
    db.executemany("DELETE FROM store.media_ref WHERE key=?", [(k,) for k in stale])
    db.commit()
    db.close()
    return files, size
