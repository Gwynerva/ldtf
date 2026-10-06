"""What an archive keeps (archive setting "scope"): posts and comments, or posts only.

An archive of posts only never touches comments: no comment trees under posts, no feed of the user's comments, no
discussion threads, none of their media. Switching an archive to posts only drops the comments it already has — the
user confirms that in the app, seeing what goes and how much space it frees. The intent lives in the settings, not in
a queue: while scope is "posts" and comment data remains, the next purge or sync of the archive drops it (under the
archive's sync lock), so a stopped app or a cancelled job never leaves an archive half switched.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .state import META_GUARD, META_LAST_SYNC, Archive, connect_ro, state_db, store_path
from .util import FILES, count_label, log, now_ts, num, plural

SCOPE_TITLES = {"all": "Посты и комментарии", "posts": "Только посты"}
COMMENT_DIRS = ("post-trees", "threads", "my-comments")   # under raw/
TRASH = ".trash-"                                          # raw/.trash-<ts>/: comment folders being deleted
COMMENT_TABLES = ("my_comments", "slices", "threads")
COMMENT_META = ("comments_backfill_done", "raw_scanned_mtime")


def comments_kept(settings: dict) -> bool:
    return settings.get("scope") != "posts"


def dropped_owners_sql(uid: Any, col: str = "owner") -> str:
    """media_use owners that exist only because of comments: their media (own, under own posts, in discussions) and
    the avatars of comment authors (the owner's own avatar stays: every page shows it)."""
    own = f"avatar:{int(uid)}" if isinstance(uid, int) or str(uid or "").isdigit() else ""
    return (f"({col} LIKE 'pc:%' OR {col} LIKE 'mc:%' OR {col} LIKE 'tc:%' "
            f"OR ({col} LIKE 'avatar:%' AND {col} != '{own}'))")


def _comment_dirs(arch: Archive) -> list[Path]:
    dirs = [arch.raw / d for d in COMMENT_DIRS if (arch.raw / d).exists()]
    return dirs + (sorted(arch.raw.glob(TRASH + "*")) if arch.raw.is_dir() else [])


def _read(arch: Archive, fn: Any, default: Any) -> Any:
    """fn(con) on a read-only connection to the archive's database (with the media store attached when present);
    `default` when there is no database or it can't be read."""
    p = state_db(arch.root)
    if not p.exists():
        return default
    try:
        con = connect_ro(p)
        try:
            sp = store_path(arch.library)
            if sp.exists():
                con.execute("ATTACH DATABASE ? AS store", (f"{sp.resolve().as_uri()}?mode=ro",))
            return fn(con)
        finally:
            con.close()
    except sqlite3.Error as e:
        log.debug(f"[{arch.nick}] комментарии архива не посчитаны: {e}")
        return default


def has_comment_data(arch: Archive) -> bool:
    """Anything of comments left in the archive: rows, raw files, comment trees marked as fetched, their media refs."""
    if _comment_dirs(arch):
        return True

    def q(con: sqlite3.Connection) -> bool:
        uid = (con.execute("SELECT value FROM meta WHERE key='user_id'").fetchone() or [None])[0]
        return bool(con.execute(
            "SELECT EXISTS(SELECT 1 FROM my_comments) OR EXISTS(SELECT 1 FROM slices) OR EXISTS(SELECT 1 FROM threads) "
            "OR EXISTS(SELECT 1 FROM posts WHERE tree_status IS NOT NULL) "
            f"OR EXISTS(SELECT 1 FROM media_use WHERE {dropped_owners_sql(uid)})").fetchone()[0])
    return _read(arch, q, False)


def pending_drop(arch: Archive) -> bool:
    """The archive keeps posts only but still has comments: the next purge / sync drops them."""
    return not comments_kept(arch.settings()) and has_comment_data(arch)


def _dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def comment_footprint(arch: Archive) -> dict[str, int]:
    """What dropping the comments would remove (read-only; for the confirmation):
    {comments, lost (kept only in the archive: DTF deleted them), trees, threads, files, bytes}. `files`/`bytes` are the
    media used only by comments of this archive; other archives may share some, so it is an upper bound."""
    out = {"comments": 0, "lost": 0, "trees": 0, "threads": 0, "files": 0, "bytes": 0}

    def q(con: sqlite3.Connection) -> dict[str, int]:
        one = lambda sql: con.execute(sql).fetchone()[0] or 0  # noqa: E731
        r = {"comments": one("SELECT COUNT(*) FROM my_comments"),
             "lost": one("SELECT COUNT(*) FROM my_comments WHERE site_state IS NOT NULL"),
             "trees": one("SELECT COUNT(*) FROM posts WHERE tree_status IS NOT NULL"),
             "threads": one("SELECT COUNT(*) FROM threads"),
             "db": one("SELECT COALESCE(SUM(LENGTH(raw)), 0) FROM my_comments")}
        has_store = con.execute("SELECT 1 FROM pragma_database_list WHERE name='store'").fetchone()
        if has_store:
            uid = (con.execute("SELECT value FROM meta WHERE key='user_id'").fetchone() or [None])[0]
            drop = dropped_owners_sql(uid, "u.owner")
            shas = ("SELECT DISTINCT m.sha256 FROM main.media_use u JOIN store.media_ref m ON m.key = u.key "
                    "WHERE m.sha256 IS NOT NULL AND {} ")
            f, b = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(b.size), 0) FROM store.blob b WHERE b.sha256 IN ({shas.format(drop)}) "
                f"AND b.sha256 NOT IN ({shas.format('NOT ' + drop)})").fetchone()
            r.update(files=f, bytes=b)
        return r
    out.update(_read(arch, q, {}))
    out["bytes"] = out.get("bytes", 0) + out.pop("db", 0) + sum(_dir_size(p) for p in _comment_dirs(arch))
    return out


def describe(fp: dict) -> str:
    """What comment_footprint found, in words: "3 496 комментариев пользователя, комментарии под 52 постами, …"."""
    parts = []
    if fp["comments"]:
        parts.append(count_label(fp["comments"], "комментарий", "комментария", "комментариев") + " пользователя")
    if fp["trees"]:
        parts.append(f"комментарии под {num(fp['trees'])} {plural(fp['trees'], 'постом', 'постами', 'постами')}")
    if fp["threads"]:
        parts.append(count_label(fp["threads"], "ветка обсуждений", "ветки обсуждений", "веток обсуждений"))
    if fp["files"]:
        parts.append(f"{count_label(fp['files'], *FILES)}, нужных только им")
    return ", ".join(parts) or "сохранённые комментарии"


def drop_comments(arch: Archive) -> dict[str, int]:
    """Delete every comment of the archive: rows, raw files, their media references (the shared store frees the files
    later: state.gc_media). The caller holds the archive's sync lock. Safe to repeat and to interrupt: the raw folders
    are first renamed out of the way (the build never reads them again), then the rows go, then the folders."""
    a = arch
    moved = []
    trash = a.raw / f"{TRASH}{now_ts()}"
    for d in COMMENT_DIRS:
        src = a.raw / d
        if src.exists():
            trash.mkdir(parents=True, exist_ok=True)
            os.replace(src, trash / d)   # a file held open by another program fails here, before anything is deleted
            moved.append(d)
    db = a.db
    uid = a.get_meta("user_id")
    n = {"comments": db.execute("SELECT COUNT(*) FROM main.my_comments").fetchone()[0],
         "trees": db.execute("SELECT COUNT(*) FROM main.posts WHERE tree_status IS NOT NULL").fetchone()[0],
         "threads": db.execute("SELECT COUNT(*) FROM main.threads").fetchone()[0]}
    for t in COMMENT_TABLES:
        db.execute(f"DELETE FROM main.{t}")
    db.execute("DELETE FROM main.history WHERE kind='comment'")   # their versions go with them
    db.execute("UPDATE main.posts SET tree_count=NULL, tree_fetched_at=NULL, tree_status=NULL, tree_error=NULL")
    db.execute(f"DELETE FROM main.meta WHERE key IN ({','.join('?' * len(COMMENT_META))})", COMMENT_META)
    g = a.get_meta(META_GUARD)
    if g and g.get("kind") == "comments-mass":
        db.execute("DELETE FROM main.meta WHERE key=?", (META_GUARD,))
    ls = a.get_meta(META_LAST_SYNC)
    if ls:   # the sync tab shows what disappeared from DTF last time: not the comments that are gone from the archive
        st = dict(ls.get("stats") or {})
        for k in ("comments", "threads"):
            st.pop(k, None)
        st["site"] = {k: v for k, v in (st.get("site") or {}).items() if not (k.startswith("comments_") or k == "context")}
        if "site_items" in st:
            st["site_items"] = [x for x in st["site_items"] if x.get("kind") != "comment"]
        a.set_meta(META_LAST_SYNC, dict(ls, stats=st))
    n["uses"] = db.execute(f"DELETE FROM main.media_use WHERE {dropped_owners_sql(uid)}").rowcount
    db.commit()
    for p in sorted(a.raw.glob(TRASH + "*")):
        shutil.rmtree(p, ignore_errors=True)
        if p.exists():
            log.warning(f"[{a.nick}] часть файлов комментариев занята другой программой ({p}) — удалю в следующий раз")
    try:   # give the space of the deleted rows back to the disk (needs about the database's size free for a moment)
        db.execute("VACUUM main")
        db.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        log.warning(f"[{a.nick}] база не сжата ({e}) — место освободится позже")
    log.info(f"[{a.nick}] комментарии удалены: своих {n['comments']}, деревьев {n['trees']}, веток {n['threads']}, "
             f"ссылок на медиа {n['uses']}")
    return n


def drop_locked(arch: Archive) -> dict[str, int]:
    """drop_comments under the archive's sync lock (a purge from the app or the CLI)."""
    from .sync import acquire_lock, release_lock   # sync imports this module
    arch.state_dir.mkdir(parents=True, exist_ok=True)
    other = acquire_lock(arch.lock_path)
    if other:
        raise RuntimeError(f"с архивом сейчас работает другой процесс (PID {other}) — удаление повторится позже")
    try:
        return drop_comments(arch)
    finally:
        release_lock(arch.lock_path)
