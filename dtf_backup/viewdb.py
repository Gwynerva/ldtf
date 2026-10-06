"""view.sqlite — what the app needs to render pages on demand, built from raw/ (+ FTS5 search).

Also useful for agents (plain SQL):
  posts(id, date, title, url, lead, cover, comments, rx, repost, unlisted, source, raw, site, donations, stats_at,
        versions)
      site = what happened on DTF while the archive kept the post: removed | wiped | gone | unavailable (guard.py);
      donations = rubles donated to the post (DTF's own sum); stats_at = when counters, reactions and donations were
      read from DTF; versions = how many versions of the post the archive keeps (history)
  comments(id, entry_id, reply_to, level, date, author, mine, feed, own_post, month, data, donation, donated, hist)
      mine = written by the archive owner; feed = present in the owner's comment feed;
      own_post = under the owner's post; data = zlib-compressed JSON (text, media, likes, rx, flags);
      donation = rubles sent with the comment to the post's author; donated = rubles the comment itself got;
      hist = number of earlier versions and events of the comment kept in history
  entries(id, title, subsite_id, subsite_name, own)      posts where the owner commented
  users(id, name, nickname, uri, avatar)
  months(ym, comments, groups, pages); month_groups(ym, pos, page, entry_id, root_id, mine, ...)
  comment_loc(id, ym, page)                              where each owner's comment is shown
  history(id, kind, item_id, entry_id, at, event, state, version_date, sig, body)   versions and removals (history.py):
      kind = post | comment; event = edit (body = the replaced version, zlib JSON) | removed (state = why) | restored
  post_blocks(post_id, idx, type, level)                every block of every post: level = full | generic |
      unsupported (blocks.block_level: how LDTF shows the type); the "Блоки DTF" page and the archive's block check
  search_docs(id, kind, ref, entry, date, author, title, body)   texts for search; kind: p = post,
      c = comment of the archive's user, o = other people's comment (ref = post/comment id)
  fts(kind, ref, entry, date, title, body, title_s, body_s)     FTS5 over search_docs (+ stemmed columns);
      vocab/vocab_tri — word dictionary for typo correction.  Query engine: dtf_backup/search/engine.py
Compressed columns: zlib.decompress(...) -> UTF-8 JSON.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import history as hist
from .blocks import block_level
from .context import ancestors, index_tree, keep_ids
from .normalize import MediaResolver, comment_text, html_to_text, media_info
from .reactions import reaction_pairs, reactions_total
from .search.engine import IndexWriter
from .state import Archive, connect_ro, pack, unpack
from .util import now_ts, read_json_gz, read_jsonl_gz, ts_month

PAGE_SIZE = 300  # my comments per month page
# raise when what a build produces changes (view.sqlite, md/, data/): the app rebuilds older archives by itself
VIEW_FORMAT = 3

VIEW_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE posts (id INTEGER PRIMARY KEY, date INTEGER, title TEXT, url TEXT, lead TEXT, cover TEXT,
    comments INTEGER, rx TEXT, repost INTEGER, unlisted INTEGER, source TEXT, raw BLOB, site TEXT,
    donations INTEGER DEFAULT 0, stats_at INTEGER, versions INTEGER DEFAULT 1, hist INTEGER DEFAULT 0);
CREATE TABLE comments (id INTEGER PRIMARY KEY, entry_id INTEGER, reply_to INTEGER, level INTEGER, date INTEGER,
    author INTEGER, mine INTEGER, feed INTEGER, own_post INTEGER, month TEXT, data BLOB,
    donation INTEGER DEFAULT 0, donated INTEGER DEFAULT 0, hist INTEGER DEFAULT 0);
CREATE INDEX comments_entry ON comments(entry_id);
CREATE INDEX comments_donation ON comments(donation) WHERE donation > 0;
CREATE INDEX comments_feed ON comments(feed, month, date);
CREATE TABLE entries (id INTEGER PRIMARY KEY, title TEXT, subsite_id INTEGER, subsite_name TEXT, own INTEGER);
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, nickname TEXT, uri TEXT, avatar TEXT);
CREATE TABLE months (ym TEXT PRIMARY KEY, comments INTEGER, groups INTEGER, pages INTEGER);
CREATE TABLE month_groups (ym TEXT, pos INTEGER, page INTEGER, entry_id INTEGER, root_id INTEGER, mine TEXT,
    last_date INTEGER, has_replies INTEGER, has_media INTEGER, own INTEGER, PRIMARY KEY (ym, pos));
CREATE INDEX month_groups_root ON month_groups(entry_id, root_id);
CREATE INDEX month_groups_page ON month_groups(ym, page);
CREATE TABLE comment_loc (id INTEGER PRIMARY KEY, ym TEXT, page INTEGER);
CREATE TABLE history (id INTEGER PRIMARY KEY, kind TEXT, item_id INTEGER, entry_id INTEGER, at INTEGER, event TEXT,
    state TEXT, version_date INTEGER, sig TEXT, body BLOB);
CREATE INDEX history_item ON history(kind, item_id, at);
CREATE INDEX history_at ON history(at);
CREATE TABLE post_blocks (post_id INTEGER, idx INTEGER, type TEXT, level TEXT, PRIMARY KEY (post_id, idx));
CREATE INDEX post_blocks_type ON post_blocks(type);
"""

Progress = Callable[[str, int, int], None]


# ---------------------------------------------------------------------- slim comments
def slim_comment(c: dict, users: dict[int, dict]) -> dict:
    a = c.get("author") or {}
    aid = a.get("id")
    if aid is not None and aid not in users:
        av = a.get("avatar") or {}
        users[aid] = {"name": a.get("name"), "nickname": a.get("nickname"), "uri": a.get("uri"),
                      "avatar": (av.get("data") or {}).get("uuid") if isinstance(av, dict) else None}
    likes = c.get("likes") or {}
    return {
        "id": c["id"], "date": c.get("date") or 0, "author": aid, "replyTo": c.get("replyTo") or 0,
        "level": c.get("level") or 0, "text": c.get("text") or "", "media": c.get("media") or [],
        "likes": likes.get("counterLikes") or 0, "reactions": reactions_total(c), "rx": reaction_pairs(c),
        "isRemoved": bool(c.get("isRemoved")), "isEdited": bool(c.get("isEdited")),
        "removedByModerator": bool(c.get("isRemovedByModerator")), "hiddenByBan": bool(c.get("isHiddenByBan")),
        "replyCount": c.get("replyCount") or 0, "entry": c.get("entry"),
        "donation": money(c.get("donation")),                        # sent with the comment to the post's author
        "donated": money((c.get("donations") or {}).get("amount")) if isinstance(c.get("donations"), dict) else 0,
        "site": (c.get("_site") or {}).get("state"),   # lost on DTF, archived text kept (guard.py)
    }


def money(v: Any) -> int:
    """Rubles as an int (DTF sends ints, null or nothing)."""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def post_donations(p: dict) -> int:
    d = p.get("donations")
    return money(d.get("amount")) if isinstance(d, dict) else 0


def post_plain_text(p: dict) -> str:
    out: list[str] = []
    for b in p.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        d = b.get("data") if isinstance(b.get("data"), dict) else {}
        for k in ("text", "subline1", "title", "description"):
            if isinstance(d.get(k), str):
                out.append(html_to_text(d[k]))
        if isinstance(d.get("items"), list):
            out.extend(html_to_text(x) for x in d["items"] if isinstance(x, str))
    return "\n".join(t for t in out if t)


def post_lead(p: dict) -> str:
    for b in p.get("blocks") or []:
        if isinstance(b, dict) and b.get("type") == "text" and not b.get("hidden"):
            t = html_to_text((b.get("data") or {}).get("text")).strip()
            if t:
                return t[:300] + ("…" if len(t) > 300 else "")
    return ""


def post_cover(p: dict, resolver: MediaResolver) -> dict | None:
    for key in ("customCover", "cover"):
        if isinstance(p.get(key), dict):
            m = media_info(p[key], resolver)
            if m:
                return m
    for b in p.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        d = b.get("data") or {}
        if b.get("type") == "media" and not b.get("hidden"):
            for it in d.get("items") or []:
                m = media_info(it.get("image"), resolver) if isinstance(it, dict) else None
                if m:
                    return m
        if b.get("type") == "video":
            th = ((d.get("video") or {}).get("data") or {}).get("thumbnail")
            m = media_info(th, resolver) if th else None
            if m:
                return m
    rd = (p.get("repostData") or {}).get("data") or {}
    if rd.get("image"):
        return media_info(rd["image"], resolver)
    return None


# ---------------------------------------------------------------------- dataset
class Dataset:
    """All raw data of one archive, loaded once and shared by the view builder and the exporters."""

    def __init__(self, arch: Archive):
        self.arch = arch
        self.prof: dict = read_json_gz(arch.raw_profile())
        self.uid: int = self.prof["id"]
        self.nick = arch.nick
        self.users: dict[Any, dict] = {}
        self.posts: list[dict] = []
        self.local_posts: set[int] = set()
        self.trees: dict[int, list[dict]] = {}
        self.threads: dict[int, list[dict]] = {}
        self.my: list[dict] = []
        self.entries: dict[int, dict] = {}
        self._index: dict[int, tuple[dict, dict] | None] = {}
        self.listed: dict[int, int] = {}
        self.listed_at: int | None = None
        self.hist: dict[tuple[str, int], tuple[int, int]] = {}   # (kind, id) -> (edits, events): history.counts
        self.history: list = []                                    # the history rows themselves
        self.history_since: int | None = None
        self.comments = arch.settings()["scope"] != "posts"   # the archive keeps comments (setting "scope")

    def load(self, progress: Progress | None = None) -> "Dataset":
        a = self.arch
        step = progress or (lambda *_: None)
        if a.exists():
            hist.import_legacy(a)   # versions LDTF 1.1-1.3 kept as files
            self.hist = hist.counts(a.db)
            self.history = hist.rows(a.db)
            self.history_since = a.get_meta(hist.META_SINCE)
        files = glob.glob(str(a.raw / "posts" / "*.json.gz"))
        for i, f in enumerate(files):
            self.posts.append(read_json_gz(Path(f)))
            step("load-posts", i + 1, len(files))
        self.posts.sort(key=lambda p: (p.get("date") or 0, p["id"]), reverse=True)
        self.local_posts = {p["id"] for p in self.posts}
        if not self.comments:   # posts only: comment files a stopped sync may have left behind are not read
            step("load-comments", 1, 1)
            return self._listing()
        for p in self.posts:
            tp = a.raw_post_tree(p["id"])
            self.trees[p["id"]] = [slim_comment(c, self.users) for c in read_json_gz(tp, {}).get("items", [])]
        seen: set[int] = set()
        my = []
        for f in sorted(glob.glob(str(a.raw / "my-comments" / "*.jsonl.gz"))):
            for c in read_jsonl_gz(Path(f)):
                my.append(slim_comment(c, self.users))
        my.sort(key=lambda c: (c["date"], c["id"]), reverse=True)
        self.my = [c for c in my if not (c["id"] in seen or seen.add(c["id"]))]  # type: ignore[func-returns-value]
        step("load-comments", 1, 1)
        tfiles = glob.glob(str(a.raw / "threads" / "*.json.gz"))
        for i, f in enumerate(tfiles):
            th = read_json_gz(Path(f))
            self.threads[th["entryId"]] = [slim_comment(c, self.users) for c in th.get("items", [])]
            if i % 500 == 0 or i + 1 == len(tfiles):
                step("load-threads", i + 1, len(tfiles))
        # one comment, two copies (its post's tree or a thread, and the owner's feed) fetched at different times:
        # donations only grow, the larger one is the fresher
        donated: dict[int, tuple[int, int]] = {}
        for items in (*self.trees.values(), *self.threads.values()):
            for c in items:
                if c["donation"] or c["donated"]:
                    a, b = donated.get(c["id"], (0, 0))
                    donated[c["id"]] = (max(a, c["donation"]), max(b, c["donated"]))
        for c in self.my:
            d = donated.get(c["id"])
            if d:
                c["donation"], c["donated"] = max(c["donation"], d[0]), max(c["donated"], d[1])
        if self.hist:   # comments with versions or removals link to their history
            for items in (*self.trees.values(), *self.threads.values(), self.my):
                for c in items:
                    h = self.hist.get(("comment", c["id"]))
                    if h:
                        c["hist"] = h[1]
        for c in self.my:
            e = c.get("entry") or {}
            if e.get("id") and e["id"] not in self.entries:
                self.entries[e["id"]] = {"title": e.get("title"), "subsiteId": e.get("subsiteId"),
                                         "subsiteName": e.get("subsiteName")}
        return self._listing()

    def _listing(self) -> "Dataset":
        a = self.arch
        if a.exists():
            self.listed_at = a.get_meta("posts_listed_at")
            self.listed = {r[0]: r[1] for r in a.db.execute("SELECT id, listed_at FROM main.posts")}
            stats = {r[0]: (r[1], r[2]) for r in a.db.execute(
                "SELECT id, stats, stats_at FROM main.posts WHERE stats IS NOT NULL")}
            for p in self.posts:
                live_stats_into(p, *stats.get(p["id"], (None, None)))
        return self

    @property
    def my_by_id(self) -> dict[int, dict]:
        if not hasattr(self, "_my_by_id"):
            self._my_by_id = {c["id"]: c for c in self.my}
        return self._my_by_id

    def entry_title(self, eid: int | None) -> str:
        if not hasattr(self, "_titles"):
            self._titles = {eid: (e.get("title") or "") for eid, e in self.entries.items()}
            self._titles.update({p["id"]: p.get("title") or "" for p in self.posts})
        return self._titles.get(eid or 0) or ""

    def entry_items(self, eid: int | None) -> list[dict] | None:
        if not eid:
            return None
        if eid in self.local_posts:
            return self.trees.get(eid, [])
        return self.threads.get(eid)

    def entry_index(self, eid: int | None) -> tuple[dict, dict] | None:
        if not eid:
            return None
        if eid not in self._index:
            items = self.entry_items(eid)
            self._index[eid] = index_tree(items) if items is not None else None
        return self._index[eid]

    def unlisted(self, pid: int) -> bool:
        return bool(self.listed_at and pid in self.listed and self.listed[pid] < self.listed_at)


def live_stats_into(p: dict, stats: str | None, at: int | None) -> None:
    """The post's counters, reactions and donations as of the latest listing (sync.live_stats); the raw post keeps
    the ones of its download. A post DTF lost keeps what it had then."""
    if not stats or p.get("_site"):
        return
    try:
        live = json.loads(stats)
    except ValueError:
        return
    if isinstance(live.get("counters"), dict):
        p["counters"] = {**(p.get("counters") or {}), **live["counters"]}
    for k in ("reactions", "donations", "likes"):
        if isinstance(live.get(k), dict):
            p[k] = live[k]
    p["_statsAt"] = at


# ---------------------------------------------------------------------- month groups
def group_months(ds: Dataset) -> "OrderedDict[str, list[dict]]":
    """My comments per month grouped by (post, root thread): each group is rendered once, so long dialogs
    don't repeat the same context for every reply. Groups keep newest-first order and are split into
    pages of ~PAGE_SIZE of my comments."""
    by_month: OrderedDict[str, list[dict]] = OrderedDict()
    for c in ds.my:
        by_month.setdefault(ts_month(c["date"]), []).append(c)
    out: OrderedDict[str, list[dict]] = OrderedDict()
    for ym, items in by_month.items():
        groups: OrderedDict[tuple, dict] = OrderedDict()
        for c in items:
            eid = (c.get("entry") or {}).get("id")
            idx = ds.entry_index(eid)
            root = c["id"]
            has_replies = False
            if idx and c["id"] in idx[0]:
                anc = ancestors(c["id"], idx[0])
                root = next((a for a in anc if a in idx[0]), c["id"])
                has_replies = bool(idx[1].get(c["id"]))
            key = (eid, root)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"ym": ym, "entry_id": eid, "root_id": root, "mine": [], "last_date": c["date"],
                                   "has_replies": False, "has_media": False, "own": bool(eid and eid in ds.local_posts)}
            g["mine"].append(c["id"])
            g["has_replies"] = g["has_replies"] or has_replies
            g["has_media"] = g["has_media"] or bool(c.get("media"))
        page, acc = 1, 0
        lst = list(groups.values())
        for pos, g in enumerate(lst):
            if acc >= PAGE_SIZE:
                page += 1
                acc = 0
            g["pos"], g["page"] = pos, page
            acc += len(g["mine"])
        out[ym] = lst
    return out


def group_view(idx: tuple[dict, dict] | None, mine_ids: list[int], root_id: int) -> dict:
    """Comments shown for a month group: ancestors + my comments + replies below them, and the flat chain
    above the first branching point (rendered collapsed)."""
    mine = set(mine_ids)
    if not idx or not any(m in idx[0] for m in mine):
        return {"standalone": True}
    by_id, children = idx
    keep = keep_ids(mine, by_id, children)
    kids = {k: [x for x in v if x in keep] for k, v in children.items() if k in keep}
    chain: list[int] = []
    cur = root_id if root_id in keep else min(keep, key=lambda i: by_id[i].get("level", 0))
    while cur not in mine and len(kids.get(cur, [])) == 1:
        chain.append(cur)
        cur = kids[cur][0]
    return {"standalone": False, "by_id": by_id, "kids": kids, "keep": keep, "chain": chain, "cur": cur}


# ---------------------------------------------------------------------- build
def build_view(ds: Dataset, groups: "OrderedDict[str, list[dict]]", resolver: MediaResolver,
               progress: Progress | None = None) -> Path:
    arch = ds.arch
    step = progress or (lambda *_: None)
    final = arch.view_path
    tmp = final.with_name(final.name + ".tmp")
    for p in (tmp, Path(str(tmp) + "-journal")):
        p.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(VIEW_SCHEMA)
    index = IndexWriter(db)

    # posts
    for p in ds.posts:
        cov = post_cover(p, resolver)
        edits, events = ds.hist.get(("post", p["id"]), (0, 0))
        db.execute("INSERT INTO posts(id, date, title, url, lead, cover, comments, rx, repost, unlisted, source, raw, "
                   "site, donations, stats_at, versions, hist) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (p["id"], p.get("date") or 0, p.get("title") or "", p.get("url") or "", post_lead(p),
                    json.dumps(cov, ensure_ascii=False) if cov else None,
                    (p.get("counters") or {}).get("comments", 0), json.dumps(reaction_pairs(p)),
                    1 if p.get("repostId") else 0, 1 if ds.unlisted(p["id"]) else 0, p.get("_source", "content"),
                    pack(p), (p.get("_site") or {}).get("state"), post_donations(p), p.get("_statsAt"), 1 + edits,
                    events))
        index.add("p", p["id"], p["id"], p.get("date") or 0, ds.uid, p.get("title") or "", post_plain_text(p))
        db.executemany("INSERT OR REPLACE INTO post_blocks VALUES (?,?,?,?)",
                       [(p["id"], i, t, block_level(t)) for i, t in
                        enumerate(str(b.get("type") or "?") if isinstance(b, dict) else "?" for b in p.get("blocks") or [])])
    step("build-posts", 1, 1)

    # comments: trees + threads first, then the owner's feed (fresher copies, month assignment)
    def ins(c: dict, eid: int | None, own: bool, feed: bool) -> None:
        db.execute("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "feed=MAX(feed, excluded.feed), month=COALESCE(excluded.month, month), "
                   "data=CASE WHEN excluded.feed=1 THEN excluded.data ELSE data END, "
                   "donation=MAX(donation, excluded.donation), donated=MAX(donated, excluded.donated), "
                   "hist=MAX(hist, excluded.hist)",
                   (c["id"], eid, c["replyTo"] or 0, c["level"], c["date"], c["author"],
                    1 if c["author"] == ds.uid else 0, 1 if feed else 0, 1 if own else 0,
                    ts_month(c["date"]) if feed else None, pack(c), c.get("donation") or 0, c.get("donated") or 0,
                    c.get("hist") or 0))

    for pid, items in ds.trees.items():
        for c in items:
            ins(c, pid, True, False)
    for eid, items in ds.threads.items():
        for c in items:
            ins(c, eid, eid in ds.local_posts, False)
    for c in ds.my:
        eid = (c.get("entry") or {}).get("id")
        ins(c, eid, bool(eid and eid in ds.local_posts), True)
    step("build-comments", 1, 1)

    my_ids = {c["id"] for c in ds.my}
    rows = db.execute("SELECT id, entry_id, date, author, data FROM comments").fetchall()
    for n, row in enumerate(rows, 1):
        c = unpack(row[4])
        index.add("c" if row[0] in my_ids else "o", row[0], row[1], row[2], row[3], "",
                  comment_text(c.get("text")).text)
        if n % 20000 == 0:
            step("build-search", n, len(rows))
    index.finish()
    step("build-search", len(rows), len(rows))

    for eid, e in ds.entries.items():
        db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                   (eid, e.get("title"), e.get("subsiteId"), e.get("subsiteName"), 1 if eid in ds.local_posts else 0))
    for p in ds.posts:
        db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,1)", (p["id"], p.get("title"), ds.uid, ds.prof.get("name")))
    for uid, u in ds.users.items():
        if isinstance(uid, int):
            db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)",
                       (uid, u.get("name"), u.get("nickname"), u.get("uri"), u.get("avatar")))

    db.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(r["id"], r["kind"], r["item_id"], r["entry_id"], r["at"], r["event"], r["state"], r["version_date"],
                     r["sig"], r["body"]) for r in ds.history if r["kind"] == "post" or ds.comments])

    for ym, lst in groups.items():
        pages = max((g["page"] for g in lst), default=1)
        db.execute("INSERT INTO months VALUES (?,?,?,?)", (ym, sum(len(g["mine"]) for g in lst), len(lst), pages))
        for g in lst:
            db.execute("INSERT INTO month_groups VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ym, g["pos"], g["page"], g["entry_id"], g["root_id"], json.dumps(g["mine"]), g["last_date"],
                        int(g["has_replies"]), int(g["has_media"]), int(g["own"])))
            for cid in g["mine"]:
                db.execute("INSERT OR REPLACE INTO comment_loc VALUES (?,?,?)", (cid, ym, g["page"]))
    step("build-months", 1, 1)

    meta = {"built_at": now_ts(), "tool_version": __version__, "view_format": VIEW_FORMAT, "uid": ds.uid, "nick": ds.nick,
            "comments": ds.comments, "history_since": ds.history_since,
            "profile": ds.prof, "counts": {"posts": len(ds.posts), "my_comments": len(ds.my),
                                           "post_comments": sum(len(v) for v in ds.trees.values()),
                                           "context_entries": len(ds.threads)}}
    db.executemany("INSERT INTO meta VALUES (?,?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    db.commit()
    db.close()
    _replace(tmp, final)
    return final


def _replace(tmp: Path, final: Path) -> None:
    """Atomic swap; the server may be reading the old file (Windows keeps it locked briefly)."""
    for attempt in range(40):
        try:
            os.replace(tmp, final)
            return
        except PermissionError:
            time.sleep(0.25)
    raise PermissionError(f"не удалось заменить {final}: файл занят")


def open_view(arch: Archive) -> sqlite3.Connection | None:
    """The built view, or None when there is none yet — or the file is empty/damaged (then the archive just looks
    unbuilt and the next render replaces it, instead of one bad file breaking every page of the app)."""
    if not arch.view_path.exists():
        return None
    try:
        db = connect_ro(arch.view_path, timeout=30)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
            db.close()
            return None
        return db
    except sqlite3.DatabaseError:
        return None


def view_ready(arch: Archive) -> bool:
    db = open_view(arch)
    if db is None:
        return False
    db.close()
    return True


def view_meta(db: sqlite3.Connection) -> dict:
    return {r[0]: json.loads(r[1]) for r in db.execute("SELECT key, value FROM meta")}


def view_outdated(arch: Archive) -> bool:
    """The archive has downloaded data but no view, or one built by an older LDTF (VIEW_FORMAT): rebuild it."""
    if not arch.raw_profile().exists():
        return False
    db = open_view(arch)
    if db is None:
        return True
    try:
        row = db.execute("SELECT value FROM meta WHERE key='view_format'").fetchone()
        return (json.loads(row[0]) if row else 1) < VIEW_FORMAT
    except (sqlite3.Error, ValueError):
        return True
    finally:
        db.close()
