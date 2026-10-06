"""The MCP tools: read-only access to the archives of one library (view.sqlite of each archive).

Every tool answers with Markdown text (archived content is mostly Russian). Long answers are cut with a note on how
to get the rest (page / limit / offset). Media are linked on DTF's CDN, links point to dtf.ru.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import html
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..api import comment_url, post_url
from ..blocks import Ctx, Report, render_blocks
from ..context import ancestors, descendants, index_tree
from ..guard import state_title
from ..normalize import Linker, MediaResolver, comment_text
from ..reactions import Reactions
from ..search.engine import has_index, search
from ..state import Archive, archive_dirs, unpack
from ..util import MSK, ts_human
from ..viewdb import open_view, post_plain_text, view_meta

MAX_CHARS = 60_000
TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"\s+|[^\s]+")


class ToolError(Exception):
    """A problem with the arguments: the agent gets it as a tool result with isError."""


def _date(s: Any, end: bool = False) -> int | None:
    if s in (None, ""):
        return None
    try:
        d = _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        raise ToolError(f"дата должна быть в формате ГГГГ-ММ-ДД, а не «{s}»") from None
    if end:
        d += _dt.timedelta(days=1)
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=MSK).timestamp())


def _plain(h: str) -> str:
    return html.unescape(TAG_RE.sub("", h or ""))


def _cut(text: str, hint: str = "") -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n…(ответ обрезан до {MAX_CHARS} символов{'; ' + hint if hint else ''})"


def _diff_text(a: str, b: str, keep: int = 160) -> str:
    """What changed between two texts: [-removed-] {+added+}, long unchanged stretches shortened to their ends."""
    ta, tb = WORD_RE.findall(a or ""), WORD_RE.findall(b or "")
    out: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            same = "".join(ta[i1:i2])
            out.append(same if len(same) <= 2 * keep else same[:keep] + " … " + same[-keep:])
            continue
        if op in ("delete", "replace"):
            out.append("[-" + "".join(ta[i1:i2]) + "-]")
        if op in ("insert", "replace"):
            out.append("{+" + "".join(tb[j1:j2]) + "+}")
    return "".join(out)


class _View:
    """What a tool needs of one archive: its view.sqlite (opened per call: a rebuild must be able to replace the file)
    and the names of its people."""

    def __init__(self, arch: Archive, rx: Reactions):
        self.arch = arch
        db = open_view(arch)
        if db is None:
            raise ToolError(f"архив @{arch.nick} ещё не собран — дождитесь окончания первой синхронизации")
        try:
            self.meta = view_meta(db)
            self.users = {r[0]: {"name": r[1], "nickname": r[2]} for r in db.execute("SELECT id, name, nickname FROM users")}
            self.local_posts = {r[0] for r in db.execute("SELECT id FROM posts")}
        finally:
            db.close()
        self.uid = self.meta["uid"]
        self.rx = rx
        self.ctx = Ctx(MediaResolver(), Linker(set(), None), "/", "", "mcp", "mcp", Report(), set())

    def db(self) -> sqlite3.Connection:
        db = open_view(self.arch)
        if db is None:
            raise ToolError(f"архив @{self.arch.nick} сейчас пересобирается — повторите через минуту")
        return db

    def who(self, aid: Any) -> str:
        u = self.users.get(aid) or {}
        name = u.get("name") or (f"id{aid}" if aid else "аноним")
        return name + (" (автор архива)" if aid == self.uid else "")

    def comment_line(self, c: dict, depth: int = 0) -> str:
        text = comment_text(c.get("text")).text.strip().replace("\n", " ")
        extra = []
        if c.get("media"):
            extra.append(f"вложений: {len(c['media'])}")
        if c.get("donation"):
            extra.append(f"донат {c['donation']} ₽")
        if c.get("site"):
            extra.append(state_title(c["site"]))
        pos, neg = self.rx.split([tuple(x) for x in c.get("rx") or []])
        rating = f" ▲{pos}" + (f" ▼{neg}" if neg else "") if pos or neg else ""
        tail = f" [{'; '.join(extra)}]" if extra else ""
        return f"{'  ' * depth}- **{self.who(c.get('author'))}** ({ts_human(c.get('date'))}, id {c['id']}{rating}): " \
               f"{text or '(без текста)'}{tail}"


class Tools:
    """The tools over one library. `view_ready(nick)` decides which archives exist (the app passes its own cache)."""

    def __init__(self, library: Path):
        self.library = library.resolve()
        self._views: dict[str, tuple[float, _View]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ archives
    def nicks(self) -> list[str]:
        out = []
        for d in archive_dirs(self.library):
            if (d / ".state" / "view.sqlite").exists():
                out.append(d.name)
        return out

    def view(self, archive: str | None) -> _View:
        nicks = self.nicks()
        if not nicks:
            raise ToolError("в библиотеке LDTF ещё нет собранных архивов")
        nick = (archive or "").strip().lstrip("@")
        if not nick:
            if len(nicks) > 1:
                raise ToolError(f"укажите archive — один из: {', '.join(nicks)}")
            nick = nicks[0]
        if nick not in nicks:
            raise ToolError(f"архива «{nick}» нет; есть: {', '.join(nicks)}")
        arch = Archive(self.library / nick, self.library)
        try:
            stamp = arch.view_path.stat().st_mtime
        except OSError:
            stamp = 0.0
        with self._lock:
            hit = self._views.get(nick)
            if hit and hit[0] == stamp:
                return hit[1]
        rx = Reactions.for_library(self.library, MediaResolver(), None)
        v = _View(arch, rx)
        with self._lock:
            self._views[nick] = (stamp, v)
        return v

    # ------------------------------------------------------------------ tools
    def list_archives(self) -> str:
        rows = []
        for nick in self.nicks():
            try:
                v = self.view(nick)
            except ToolError as e:
                rows.append(f"- **@{nick}** — {e}")
                continue
            c = v.meta.get("counts") or {}
            prof = v.meta.get("profile") or {}
            built = ts_human(v.meta.get("built_at"))
            line = (f"- **@{nick}** — {prof.get('name') or nick} (DTF id {v.uid}): постов {c.get('posts', 0)}"
                    + (f", своих комментариев {c.get('my_comments', 0)}, комментариев под постами "
                       f"{c.get('post_comments', 0)}" if v.meta.get("comments", True) else ", только посты")
                    + f"; собран {built}")
            rows.append(line)
        if not rows:
            return "Архивов пока нет."
        return "Архивы LDTF:\n" + "\n".join(rows)

    def search(self, query: str, archive: str | None = None, where: str = "all", year: int | None = None,
               date_from: str | None = None, date_to: str | None = None, sort: str = "relevance", limit: int = 20,
               page: int = 1) -> str:
        v = self.view(archive)
        kind = {"all": "", "posts": "p", "my_comments": "c", "others_comments": "o"}.get(where)
        if kind is None:
            raise ToolError("where: all | posts | my_comments | others_comments")
        a, b = _date(date_from), _date(date_to, end=True)
        if year:
            a = a or _date(f"{int(year)}-01-01")
            b = b or _date(f"{int(year) + 1}-01-01")
        limit = max(1, min(int(limit or 20), 50))
        page = max(1, int(page or 1))
        db = v.db()
        try:
            if not has_index(db):
                raise ToolError("у архива нет поискового индекса — пересоберите его в LDTF")
            res = search(db, query, kind, a, b, {"relevance": "rank", "newest": "date", "oldest": "old"}.get(sort, "rank"),
                         page, limit)
            if res.error:
                raise ToolError(res.error)
            ids = [h.ref for h in res.hits if h.kind != "p"]
            parents: dict[int, dict] = {}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in db.execute(f"SELECT id, entry_id, data FROM comments WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                    parents[r[0]] = {"entry": r[1], **unpack(r[2])}
            titles = {}
            eids = list({h.entry for h in res.hits if h.entry})
            for i in range(0, len(eids), 500):
                chunk = eids[i:i + 500]
                titles.update(dict(db.execute(f"SELECT id, title FROM entries WHERE id IN ({','.join('?' * len(chunk))})",
                                              chunk).fetchall()))
        finally:
            db.close()
        out = [f"Найдено: {res.total} (страница {page}, по {limit})" + (f"; исправлено: {'; '.join(res.notes)}" if res.notes else "")]
        if res.suggestion:
            out.append(f"Возможно, имелось в виду: {res.suggestion}")
        for h in res.hits:
            snip = _plain(res.marker.snippet(h.body, 400)[0]) if res.marker else h.body[:400]
            if h.kind == "p":
                out.append(f"\n### Пост {h.ref}: {h.title}\n{ts_human(h.date)} · {post_url(h.ref)}\n{snip}")
            else:
                c = parents.get(h.ref) or {}
                who = "автор архива" if h.kind == "c" else v.who(h.author)
                post = f"«{titles[h.entry]}»" if titles.get(h.entry) else "(название не сохранено)"
                out.append(f"\n### Комментарий {h.ref} ({who})\n{ts_human(h.date)} · в посте {post} "
                           f"(post_id {h.entry}) · {comment_url(h.entry, h.ref) if h.entry else ''}"
                           + (f" · в ответ на комментарий {c['replyTo']}" if c.get("replyTo") else "") + f"\n{snip}")
        if res.total > page * limit:
            out.append(f"\nЕщё результаты: page={page + 1}")
        return _cut("\n".join(out), "сузьте запрос или уменьшите limit")

    def get_post(self, post_id: int, archive: str | None = None, version: int | None = None, comments: bool = False,
                 comments_limit: int = 200, comments_offset: int = 0) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            row = db.execute("SELECT * FROM posts WHERE id=?", (int(post_id),)).fetchone()
            if row is None:
                raise ToolError(f"поста {post_id} в архиве @{v.arch.nick} нет (это посты автора архива; чужие посты — "
                                f"только комментарии в get_comment)")
            p = unpack(row["raw"])
            hist = db.execute("SELECT id, at, event, state, version_date, body FROM history WHERE kind='post' AND item_id=? "
                              "ORDER BY at, id", (int(post_id),)).fetchall()
            items = [unpack(r[0]) for r in db.execute("SELECT data FROM comments WHERE entry_id=? AND own_post=1",
                                                      (int(post_id),))] if comments else []
        finally:
            db.close()
        note = ""
        if version is not None:
            h = next((r for r in hist if r["id"] == int(version) and r["event"] == "edit"), None)
            if h is None:
                raise ToolError(f"у поста {post_id} нет версии {version}; версии — в get_history")
            p = dict(p, **unpack(h["body"]))
            note = f"Прежняя версия от {ts_human(h['version_date'])} (заменена правкой, замеченной {ts_human(h['at'])})\n"
        pairs = [tuple(x) for x in json.loads(row["rx"] or "[]")]
        pos, neg = v.rx.split(pairs)
        counters = p.get("counters") or {}
        _, md, _ = render_blocks(p.get("blocks") or [], v.ctx)
        meta = [f"id {post_id}", ts_human(p.get("date")), p.get("url") or post_url(int(post_id)),
                f"комментариев {counters.get('comments', 0)}", f"реакции ▲{pos} ▼{neg}"]
        if row["donations"]:
            meta.append(f"донаты {row['donations']} ₽")
        if row["site"]:
            meta.append(state_title(row["site"]) + " (в архиве сохранена прежняя версия)")
        edits = [r for r in hist if r["event"] == "edit"]
        if edits:
            meta.append(f"версий {len(edits) + 1} (get_history)")
        out = [f"# {p.get('title') or 'Без заголовка'}", note + " · ".join(meta), "", md]
        rd = (p.get("repostData") or {}).get("data")
        if rd:
            out.insert(2, f"Репост: «{rd.get('title') or ''}» {rd.get('url') or ''}")
        if comments:
            by_id, children = index_tree(items)
            roots = [c["id"] for c in sorted(items, key=lambda c: (c["date"], c["id"]))
                     if not c["replyTo"] or c["replyTo"] not in by_id]
            lines: list[str] = []

            def rec(cid: int, depth: int) -> None:
                lines.append(v.comment_line(by_id[cid], depth))
                for k in children.get(cid, []):
                    if k in by_id:
                        rec(k, depth + 1)
            for r in roots:
                rec(r, 0)
            off = max(0, int(comments_offset or 0))
            lim = max(1, min(int(comments_limit or 200), 1000))
            part = lines[off:off + lim]
            out += ["", f"## Комментарии ({len(items)}; показаны {off + 1}–{off + len(part)})"] + part
            if off + lim < len(lines):
                out.append(f"\nЕщё: comments_offset={off + lim}")
        return _cut("\n".join(out), "комментарии — по частям: comments_offset / comments_limit")

    def get_comment(self, comment_id: int, archive: str | None = None, context: bool = True) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            row = db.execute("SELECT entry_id, data FROM comments WHERE id=?", (int(comment_id),)).fetchone()
            if row is None:
                raise ToolError(f"комментария {comment_id} в архиве @{v.arch.nick} нет")
            eid = row["entry_id"]
            items = [unpack(r[0]) for r in db.execute("SELECT data FROM comments WHERE entry_id=?", (eid,))] if context else []
            title = db.execute("SELECT title FROM entries WHERE id=?", (eid,)).fetchone()
        finally:
            db.close()
        c = unpack(row["data"])
        out = [f"Комментарий {comment_id} в посте «{(title[0] if title else '') or eid}» (post_id {eid}, "
               f"{'пост автора архива' if eid in v.local_posts else 'чужой пост'}) · {comment_url(eid, int(comment_id))}"]
        if c.get("hist"):
            out.append(f"У комментария есть история: get_history kind=comment id={comment_id}")
        if not context:
            return "\n".join(out + ["", v.comment_line(c)])
        by_id, children = index_tree(items)
        anc = [a for a in ancestors(int(comment_id), by_id) if a in by_id]
        out.append("\n## Ветка выше" if anc else "")
        out += [v.comment_line(by_id[a], i) for i, a in enumerate(anc)]
        out += ["\n## Комментарий", v.comment_line(c, len(anc))]
        reps = descendants(int(comment_id), children)
        if reps:
            out.append(f"\n## Ответы ({len(reps)})")

            def rec(cid: int, depth: int) -> None:
                for k in children.get(cid, []):
                    if k in by_id:
                        out.append(v.comment_line(by_id[k], depth))
                        rec(k, depth + 1)
            rec(int(comment_id), len(anc) + 1)
        return _cut("\n".join(x for x in out if x is not None))

    def list_posts(self, archive: str | None = None, sort: str = "new", date_from: str | None = None,
                   date_to: str | None = None, limit: int = 50, offset: int = 0) -> str:
        v = self.view(archive)
        order = {"new": "date DESC", "old": "date ASC", "comments": "comments DESC", "donations": "donations DESC",
                 "reactions": None}.get(sort, "date DESC")
        a, b = _date(date_from), _date(date_to, end=True)
        where, args = ["1"], []
        if a is not None:
            where.append("date >= ?")
            args.append(a)
        if b is not None:
            where.append("date < ?")
            args.append(b)
        db = v.db()
        try:
            rows = [dict(r) for r in db.execute(f"SELECT id, date, title, url, comments, rx, donations, site, versions "
                                                f"FROM posts WHERE {' AND '.join(where)} ORDER BY {order or 'date DESC'}",
                                                args)]
        finally:
            db.close()
        for r in rows:
            r["pos"], r["neg"] = v.rx.split([tuple(x) for x in json.loads(r["rx"] or "[]")])
        if order is None:
            rows.sort(key=lambda r: -r["pos"])
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        part = rows[offset:offset + limit]
        out = [f"Постов: {len(rows)}; показаны {offset + 1}–{offset + len(part)} (сортировка {sort})"]
        for r in part:
            extra = [f"комм. {r['comments']}", f"▲{r['pos']} ▼{r['neg']}"]
            if r["donations"]:
                extra.append(f"донаты {r['donations']} ₽")
            if r["versions"] and r["versions"] > 1:
                extra.append(f"версий {r['versions']}")
            if r["site"]:
                extra.append(state_title(r["site"]))
            out.append(f"- {ts_human(r['date'])} · id {r['id']} · **{r['title'] or 'Без заголовка'}** · {', '.join(extra)}")
        if offset + limit < len(rows):
            out.append(f"\nЕщё: offset={offset + limit}")
        return _cut("\n".join(out))

    def list_comments(self, archive: str | None = None, sort: str = "new", date_from: str | None = None,
                      date_to: str | None = None, limit: int = 50, offset: int = 0) -> str:
        v = self.view(archive)
        a, b = _date(date_from), _date(date_to, end=True)
        where, args = ["mine=1"], []
        if a is not None:
            where.append("date >= ?")
            args.append(a)
        if b is not None:
            where.append("date < ?")
            args.append(b)
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        db = v.db()
        try:
            total = db.execute(f"SELECT COUNT(*) FROM comments WHERE {' AND '.join(where)}", args).fetchone()[0]
            rows = db.execute(f"SELECT id, entry_id, data FROM comments WHERE {' AND '.join(where)} ORDER BY date "
                              f"{'ASC' if sort == 'old' else 'DESC'} LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
            eids = list({r[1] for r in rows if r[1]})
            titles = dict(db.execute(f"SELECT id, title FROM entries WHERE id IN ({','.join('?' * len(eids))})",
                                     eids).fetchall()) if eids else {}
        finally:
            db.close()
        out = [f"Комментариев автора: {total}; показаны {offset + 1}–{offset + len(rows)}"]
        for r in rows:
            c = unpack(r[2])
            out.append(v.comment_line(c) + f" — в посте «{titles.get(r[1]) or r[1]}» (post_id {r[1]})")
        if offset + limit < total:
            out.append(f"\nЕщё: offset={offset + limit}")
        return _cut("\n".join(out))

    def get_history(self, kind: str, id: int, archive: str | None = None) -> str:   # noqa: A002 - the tool's argument
        if kind not in ("post", "comment"):
            raise ToolError("kind: post | comment")
        v = self.view(archive)
        db = v.db()
        try:
            rows = db.execute("SELECT * FROM history WHERE kind=? AND item_id=? ORDER BY at, id", (kind, int(id))).fetchall()
            cur_row = (db.execute("SELECT raw FROM posts WHERE id=?", (int(id),)).fetchone() if kind == "post" else
                       db.execute("SELECT data FROM comments WHERE id=?", (int(id),)).fetchone())
        finally:
            db.close()
        since = v.meta.get("history_since")
        if not rows:
            return (f"У {'поста' if kind == 'post' else 'комментария'} {id} нет истории: правок и удалений не было"
                    + (f" (LDTF следит за ними с {ts_human(since)})" if since else "") + ".")
        cur = unpack(cur_row[0]) if cur_row else None

        def text_of(x: dict) -> str:
            return (x.get("title", "") + "\n" + post_plain_text(x)) if kind == "post" else comment_text(x.get("text")).text
        versions = [unpack(r["body"]) for r in rows if r["event"] == "edit"]
        texts = [text_of(x) for x in versions] + ([text_of(cur)] if cur else [])
        out = [f"История {'поста' if kind == 'post' else 'комментария'} {id}" + (f" (с {ts_human(since)})" if since else "")]
        i = 0
        for r in rows:
            if r["event"] == "edit":
                out.append(f"\n## Версия {r['id']} от {ts_human(r['version_date'])} — заменена правкой "
                           f"({ts_human(r['at'])})\nЧто изменилось в следующей версии ([-удалено-] {{+добавлено+}}):\n"
                           f"{_diff_text(texts[i], texts[i + 1]) if i + 1 < len(texts) else texts[i]}")
                i += 1
            elif r["event"] == "removed":
                out.append(f"\n- {ts_human(r['at'])}: {state_title(r['state']) if r['state'] else 'пропал с DTF'} "
                           f"(в архиве осталась прежняя версия)")
            else:
                out.append(f"\n- {ts_human(r['at'])}: снова есть на DTF")
        if kind == "post":
            out.append("\nПолный текст версии: get_post с version=<id версии>.")
        return _cut("\n".join(out))

    def archive_stats(self, archive: str | None = None) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            one = lambda q, *a: db.execute(q, a).fetchone()   # noqa: E731
            posts = one("SELECT COUNT(*), MIN(date), MAX(date), SUM(comments), SUM(donations) FROM posts")
            mine = one("SELECT COUNT(*), MIN(date), MAX(date) FROM comments WHERE mine=1")
            years = db.execute("SELECT strftime('%Y', date, 'unixepoch', '+3 hours') AS y, COUNT(*) FROM comments "
                               "WHERE mine=1 GROUP BY y ORDER BY y").fetchall()
            pyears = db.execute("SELECT strftime('%Y', date, 'unixepoch', '+3 hours') AS y, COUNT(*) FROM posts "
                                "GROUP BY y ORDER BY y").fetchall()
            rx_rows = [(r[0], r[1], r[2], json.loads(r[3] or "[]")) for r in
                       db.execute("SELECT id, title, comments, rx FROM posts")]
            don = one("SELECT COUNT(*), COALESCE(SUM(donation), 0) FROM comments WHERE own_post=1 AND donation>0")
            # as the donations page: DTF's sum per post, or its donation comments where DTF shows 0 (older posts)
            got = one("SELECT COALESCE(SUM(MAX(p.donations, COALESCE((SELECT SUM(c.donation) FROM comments c "
                      "WHERE c.entry_id=p.id AND c.own_post=1 AND c.donation>0), 0))), 0) FROM posts p")[0]
            top_don = db.execute("SELECT id, title, donations FROM posts WHERE donations>0 ORDER BY donations DESC LIMIT 5").fetchall()
            donors = db.execute("SELECT author, SUM(donation), COUNT(*) FROM comments WHERE own_post=1 AND donation>0 "
                                "GROUP BY author ORDER BY 2 DESC LIMIT 5").fetchall()
            hist = dict(db.execute("SELECT event, COUNT(*) FROM history GROUP BY event").fetchall())
        finally:
            db.close()
        prof = v.meta.get("profile") or {}
        scored = sorted(((v.rx.split([tuple(x) for x in rx]), pid, t, n) for pid, t, n, rx in rx_rows),
                        key=lambda x: -x[0][0])
        out = [f"# @{v.arch.nick} — {prof.get('name') or ''}",
               f"Постов: {posts[0]} ({ts_human(posts[1])} — {ts_human(posts[2])}), комментариев под ними: {posts[3] or 0}"]
        if mine[0]:
            out.append(f"Комментариев автора: {mine[0]} ({ts_human(mine[1])} — {ts_human(mine[2])})")
            out.append("По годам: " + ", ".join(f"{y}: {n}" for y, n in years))
        out.append("Посты по годам: " + ", ".join(f"{y}: {n}" for y, n in pyears))
        out.append("\n## Самые популярные посты (▲ реакции)")
        out += [f"- id {pid}: {t or 'Без заголовка'} — ▲{s[0]} ▼{s[1]}, комм. {n}" for s, pid, t, n in scored[:5]]
        if got or don[0]:
            out.append(f"\n## Донаты\nПостам: {got} ₽ (сумма DTF, у старых постов — по донат-комментариям); "
                       f"донат-комментариев: {don[0]} на {don[1]} ₽")
            out += [f"- id {pid}: {t or 'Без заголовка'} — {s} ₽" for pid, t, s in top_don]
            if donors:
                out.append("Донатеры: " + ", ".join(f"{v.who(a)} — {s} ₽ ({n})" for a, s, n in donors))
        if hist:
            out.append(f"\n## Изменения на DTF\nправок {hist.get('edit', 0)}, удалений {hist.get('removed', 0)}, "
                       f"возвращений {hist.get('restored', 0)}")
        return "\n".join(out)


# ---------------------------------------------------------------------- definitions (tools/list)
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


ARCHIVE = {"type": "string", "description": "Archive nickname (see list_archives). Optional when the library has one."}
DATE = {"type": "string", "description": "YYYY-MM-DD (Moscow time)"}
TOOLS: list[dict] = [
    {"name": "list_archives", "title": "Архивы LDTF",
     "description": "List the archived DTF.ru profiles of this LDTF library: nickname, name, number of posts and "
                    "comments, when built. Call first to get the `archive` argument for other tools.",
     "inputSchema": _schema({})},
    {"name": "search", "title": "Поиск",
     "description": "Full-text search over an archive's posts and comments (Russian morphology, typo and keyboard "
                    "layout correction). Query syntax: words (all forms), \"exact phrase\", -exclude, a OR b, prefix*. "
                    "Returns hits with ids, dates, DTF links and snippets.",
     "inputSchema": _schema({"query": {"type": "string", "description": "What to find"}, "archive": ARCHIVE,
                             "where": {"type": "string", "enum": ["all", "posts", "my_comments", "others_comments"],
                                       "default": "all"},
                             "year": {"type": "integer"}, "date_from": DATE, "date_to": DATE,
                             "sort": {"type": "string", "enum": ["relevance", "newest", "oldest"], "default": "relevance"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                             "page": {"type": "integer", "minimum": 1, "default": 1}}, ["query"])},
    {"name": "get_post", "title": "Пост",
     "description": "One post of the archive owner as Markdown: text, media links, reactions, donations, optionally "
                    "its comment tree. `version`: an earlier version id from get_history.",
     "inputSchema": _schema({"post_id": {"type": "integer"}, "archive": ARCHIVE,
                             "version": {"type": "integer", "description": "History version id (earlier text)"},
                             "comments": {"type": "boolean", "default": False},
                             "comments_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                             "comments_offset": {"type": "integer", "minimum": 0, "default": 0}}, ["post_id"])},
    {"name": "get_comment", "title": "Комментарий с контекстом",
     "description": "A comment with its thread: the comments above it and all replies below (as archived), with the "
                    "post it belongs to.",
     "inputSchema": _schema({"comment_id": {"type": "integer"}, "archive": ARCHIVE,
                             "context": {"type": "boolean", "default": True}}, ["comment_id"])},
    {"name": "list_posts", "title": "Посты",
     "description": "The archive owner's posts with dates, comment counts, reactions (▲ positive / ▼ dislikes), "
                    "donations; sortable and filterable by date.",
     "inputSchema": _schema({"archive": ARCHIVE,
                             "sort": {"type": "string", "enum": ["new", "old", "reactions", "comments", "donations"],
                                      "default": "new"},
                             "date_from": DATE, "date_to": DATE,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
    {"name": "list_comments", "title": "Комментарии автора",
     "description": "Comments written by the archive owner anywhere on DTF, newest or oldest first, with the post "
                    "each one is under. Use get_comment for the thread around one.",
     "inputSchema": _schema({"archive": ARCHIVE, "sort": {"type": "string", "enum": ["new", "old"], "default": "new"},
                             "date_from": DATE, "date_to": DATE,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
    {"name": "get_history", "title": "История правок",
     "description": "Edits, removals and returns of a post or comment seen by LDTF, with what changed between "
                    "versions ([-removed-] {+added+}).",
     "inputSchema": _schema({"kind": {"type": "string", "enum": ["post", "comment"]}, "id": {"type": "integer"},
                             "archive": ARCHIVE}, ["kind", "id"])},
    {"name": "archive_stats", "title": "Статистика архива",
     "description": "Overview of an archive: post and comment counts by year, most popular posts, donations and "
                    "donors, changes seen on DTF.",
     "inputSchema": _schema({"archive": ARCHIVE})},
]
for _t in TOOLS:
    _t["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def call(tools: Tools, name: str, args: dict) -> tuple[str, bool]:
    """(text, is_error) of a tool call."""
    fn: Callable[..., str] | None = getattr(tools, name, None) if any(t["name"] == name for t in TOOLS) else None
    if fn is None:
        raise KeyError(name)
    allowed = set(next(t for t in TOOLS if t["name"] == name)["inputSchema"]["properties"])
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"Неизвестные аргументы: {', '.join(unknown)}", True
    try:
        return fn(**args), False
    except ToolError as e:
        return f"Ошибка: {e}", True
    except (TypeError, ValueError) as e:
        return f"Ошибка в аргументах: {e}", True


SERVER_INFO = {"name": "ldtf", "title": "LDTF — локальный архив DTF", "version": __version__}
INSTRUCTIONS = ("LDTF keeps offline archives of DTF.ru profiles (Russian gaming and culture site): the owner's posts, "
                "their comments anywhere on the site with the threads around them, reactions, donations, edits and "
                "removals. Start with list_archives; use search to find things, get_post / get_comment to read them, "
                "list_posts / list_comments to browse, get_history for edits, archive_stats for an overview. "
                "Read-only: nothing here changes the archives or DTF.")
