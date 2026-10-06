"""Versions of posts and comments: what DTF changed or removed since the archive saw it.

The archive always shows the latest version it has (raw/ and state.sqlite, unchanged by this module). Every version a
sync replaces, and every removal or return on DTF, becomes a row of `history` in the archive's state.sqlite:

  history(id, kind 'post'|'comment', item_id, entry_id, at, event, state, version_date, sig, body)
    edit      body = the version that was replaced (zlib JSON, content only: title, blocks / text, media),
              version_date = when DTF says it was written, at = when a sync noticed the change, sig = its content_sig
    removed   state = why (guard.py: removed, wiped, gone, moderator, author-deleted, ...); the archive keeps the
              version it had and marks it (`_site`)
    restored  the item is on DTF again

Only changed versions are kept, compressed, without counters or author data: a version of a post costs a few KB.
Media are referenced by uuid and live in the shared store, so an edit that keeps its pictures adds no files; the
pictures an edit removed stay (media_use owners are only ever added for posts and comments).
A version counts as changed when its content signature changes: counters, reactions, donations, online marks and
other live data DTF sends with every answer never make a version.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .guard import comment_stub, post_stub
from .state import pack, unpack
from .util import log, now_ts, read_json_gz

E = html.escape

META_SINCE = "history_since"   # since when this archive keeps versions (older edits were never seen)

# live data DTF sends with every answer: never a reason for a new version
VOLATILE = {"counters", "reactions", "likes", "donations", "donation", "donate", "gifts", "isOnline", "lastModificationDate",
            "collectibles", "isFavorited", "dateFavorite", "isLiked", "isVoted", "votes", "viewsCount", "hits",
            "base64preview", "color", "hash", "size", "external_service", "isPinned", "commentsSeenCount",
            "subscribedToTreads", "isSubscribed", "online", "_site", "_source"}
TEXT_KEYS = ("text", "title", "subline1", "subline2", "description", "name", "description2")


# ---------------------------------------------------------------------- content and signatures
def _scrub(x: Any) -> Any:
    if isinstance(x, dict):
        if x.get("uuid") and isinstance(x.get("uuid"), str):
            return {"uuid": x["uuid"]}
        if x.get("type") == "osnovaEmbed" or "original_id" in x:   # an embedded post: its counters live on
            oid = x.get("original_id") or (x.get("data") or {}).get("original_id")
            if oid:
                return {"embed": oid}
        return {k: _scrub(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list):
        return [_scrub(v) for v in x]
    if isinstance(x, str):
        return " ".join(x.split())
    return x


def post_content(p: dict) -> dict:
    rd = (p.get("repostData") or {}).get("data") or {}
    return {"title": p.get("title") or "", "blocks": p.get("blocks") or [], "customCover": p.get("customCover"),
            "repost": p.get("repostId") or rd.get("id")}


def comment_content(c: dict) -> dict:
    return {"text": c.get("text") or "", "media": c.get("media") or []}


def content_sig(kind: str, obj: dict) -> str:
    body = post_content(obj) if kind == "post" else comment_content(obj)
    raw = json.dumps(_scrub(body), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def block_sig(b: Any) -> str:
    raw = json.dumps(_scrub(b), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def snapshot(kind: str, obj: dict) -> dict:
    """What a version keeps: enough to show it again (blocks as DTF sent them), nothing that changes by itself."""
    if kind == "post":
        rd = (obj.get("repostData") or {}).get("data")
        return {"id": obj.get("id"), "title": obj.get("title") or "", "blocks": obj.get("blocks") or [],
                "customCover": obj.get("customCover"), "date": obj.get("date"), "dateModified": obj.get("dateModified"),
                "url": obj.get("url"), "repostId": obj.get("repostId"),
                "repostData": {"type": (obj.get("repostData") or {}).get("type"), "data": rd} if rd else None}
    e = obj.get("entry") or {}
    return {"id": obj.get("id"), "text": obj.get("text") or "", "media": obj.get("media") or [], "date": obj.get("date"),
            "lastModificationDate": obj.get("lastModificationDate"), "isEdited": obj.get("isEdited"),
            "replyTo": obj.get("replyTo") or 0, "level": obj.get("level") or 0,
            "author": {"id": (obj.get("author") or {}).get("id")},
            "entry": {"id": e.get("id"), "title": e.get("title")} if e else None}


def version_date(kind: str, obj: dict) -> int | None:
    if kind == "post":
        return obj.get("dateModified") or obj.get("date")
    lm, d = obj.get("lastModificationDate"), obj.get("date")
    return lm if (obj.get("isEdited") and lm) else d


def is_stub(kind: str, obj: dict) -> bool:
    return bool(post_stub(obj) if kind == "post" else comment_stub(obj))


# ---------------------------------------------------------------------- recording (sync, main thread)
def ensure(db: sqlite3.Connection) -> None:
    """Remember since when the archive keeps versions (the table itself is part of state.SCHEMA)."""
    if db.execute("SELECT 1 FROM main.meta WHERE key=?", (META_SINCE,)).fetchone() is None:
        db.execute("INSERT INTO main.meta(key, value) VALUES (?, ?)", (META_SINCE, json.dumps(now_ts())))


def edited(kind: str, old: dict | None, new: dict) -> bool:
    """`new` is another version of the content the archive had (not a placeholder, not the first sighting)."""
    if not old or is_stub(kind, old) or is_stub(kind, new):
        return False
    return content_sig(kind, old) != content_sig(kind, new)


def record_edit(db: sqlite3.Connection, kind: str, old: dict, new: dict, at: int | None = None,
                entry_id: int | None = None) -> bool:
    """Keep `old` as a version when `new` changed the content. A version seen twice (a comment in its thread and in
    the owner's feed, one edit noticed by two syncs) is kept once."""
    if not edited(kind, old, new):
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO history(kind, item_id, entry_id, at, event, version_date, sig, body) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (kind, old["id"], entry_id, at or now_ts(), "edit", version_date(kind, old), content_sig(kind, old),
         pack(snapshot(kind, old))))
    return cur.rowcount > 0


def record_event(db: sqlite3.Connection, kind: str, item_id: int, event: str, state: str | None = None,
                 at: int | None = None, entry_id: int | None = None) -> bool:
    """removed / restored, each only when it changes what is known (a removal seen again by the next sync, or by the
    feed and the thread of one comment, is one event; a return needs a removal before it)."""
    last = db.execute("SELECT event FROM history WHERE kind=? AND item_id=? AND event IN ('removed','restored') "
                      "ORDER BY at DESC, id DESC LIMIT 1", (kind, item_id)).fetchone()
    last_ev = last[0] if last else None
    if event == last_ev or (event == "restored" and last_ev != "removed"):
        return False
    db.execute("INSERT INTO history(kind, item_id, entry_id, at, event, state) VALUES (?,?,?,?,?,?)",
               (kind, item_id, entry_id, at or now_ts(), event, state))
    return True


def apply_events(db: sqlite3.Connection, events: list[tuple], at: int, entry_id: int | None = None) -> int:
    """What guard.merge_items saw in a tree or thread: ("edit", old, new) | ("removed", old, state) | ("restored", old)."""
    n = 0
    for ev in events:
        c = ev[1]
        eid = entry_id or (c.get("entry") or {}).get("id")
        if ev[0] == "edit":
            n += record_edit(db, "comment", c, ev[2], at, eid)
        elif ev[0] == "removed":
            n += record_event(db, "comment", c["id"], "removed", ev[2], at, eid)
        elif ev[0] == "restored":
            n += record_event(db, "comment", c["id"], "restored", None, at, eid)
    return n


# ---------------------------------------------------------------------- versions saved by LDTF before 1.4
def import_legacy(arch: Any) -> int:
    """raw/history/posts/<id>/<dateModified>.json.gz (full copies of replaced posts, LDTF 1.1-1.3) become history
    rows; the files go only after the rows are committed and read back."""
    root: Path = arch.raw / "history" / "posts"
    if not root.is_dir():
        return 0
    db = arch.db
    ensure(db)
    files = sorted(root.glob("*/*.json.gz"))
    n = 0
    oldest = None
    for f in files:
        try:
            old = read_json_gz(f)
            pid = int(f.parent.name)
        except (OSError, ValueError):
            log.warning(f"[история] не читается {f}, оставлен как есть")
            continue
        at = int(f.stat().st_mtime)
        oldest = min(oldest or at, at)
        sig = content_sig("post", old)
        db.execute("INSERT OR IGNORE INTO history(kind, item_id, entry_id, at, event, version_date, sig, body) "
                   "VALUES ('post',?,?,?,?,?,?,?)", (pid, pid, at, "edit", version_date("post", old), sig,
                                                     pack(snapshot("post", old))))
        n += 1
    if oldest:
        since = db.execute("SELECT value FROM main.meta WHERE key=?", (META_SINCE,)).fetchone()
        if since is None or json.loads(since[0]) > oldest:
            db.execute("INSERT INTO main.meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (META_SINCE, json.dumps(oldest)))
    db.commit()
    gone = 0
    for f in files:
        try:
            old = read_json_gz(f)
            ok = db.execute("SELECT 1 FROM history WHERE kind='post' AND item_id=? AND sig=? AND event='edit'",
                            (int(f.parent.name), content_sig("post", old))).fetchone()
        except (OSError, ValueError):
            continue
        if ok:
            f.unlink()
            gone += 1
    for d in sorted(root.glob("*"), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    for d in (root, root.parent):
        try:
            d.rmdir()
        except OSError:
            pass
    if n:
        log.info(f"[история] версии постов из raw/history перенесены в историю архива: {n} (файлов удалено: {gone})")
    return n


def counts(db: sqlite3.Connection) -> dict[tuple[str, int], tuple[int, int]]:
    """{(kind, id): (edits, all events)} — for the build; {} for an archive without history yet."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history'").fetchone() is None:
        return {}
    return {(r[0], r[1]): (r[2], r[3]) for r in db.execute(
        "SELECT kind, item_id, SUM(event='edit'), COUNT(*) FROM history GROUP BY kind, item_id")}


def rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history'").fetchone() is None:
        return []
    return db.execute("SELECT * FROM history ORDER BY at, id").fetchall()


def body(row: Any) -> dict | None:
    return unpack(row["body"]) if row["body"] else None


# ---------------------------------------------------------------------- diffs (pages)
_WORD = re.compile(r"\s+|[^\s]+")


def word_diff(a: str, b: str) -> str:
    """HTML of `b` with what changed since `a`: <del>removed</del> <ins>added</ins> (escaped text)."""
    ta, tb = _WORD.findall(a or ""), _WORD.findall(b or "")
    out: list[str] = []
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.append(E("".join(ta[i1:i2])))
            continue
        if op in ("delete", "replace"):
            out.append(f'<del>{E("".join(ta[i1:i2]))}</del>')
        if op in ("insert", "replace"):
            out.append(f'<ins>{E("".join(tb[j1:j2]))}</ins>')
    return "".join(out)


def block_ops(old: list, new: list) -> list[tuple[str, list[int], list[int]]]:
    """How the blocks of two versions line up: (op, old indexes, new indexes), op = equal | replace | delete | insert."""
    sa, sb = [block_sig(b) for b in old], [block_sig(b) for b in new]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        out.append((op, list(range(i1, i2)), list(range(j1, j2))))
    return out


def summary(old: list, new: list, title_changed: bool = False) -> str:
    """"заголовок, +2 блока, −1, изменено 3" — one line about an edit of a post."""
    add = rem = chg = 0
    for op, a, b in block_ops(old, new):
        if op == "insert":
            add += len(b)
        elif op == "delete":
            rem += len(a)
        elif op == "replace":
            chg += min(len(a), len(b))
            add += max(0, len(b) - len(a))
            rem += max(0, len(a) - len(b))
    parts = ["заголовок"] if title_changed else []
    if chg:
        parts.append(f"изменено блоков: {chg}")
    if add:
        parts.append(f"добавлено: {add}")
    if rem:
        parts.append(f"удалено: {rem}")
    return ", ".join(parts) or "оформление"
