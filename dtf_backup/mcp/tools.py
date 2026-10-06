"""The MCP tools: read-only access to the archives of one library (view.sqlite of each archive).

Every tool answers with Markdown text (archived content is mostly Russian). Answers stay within MAX_CHARS (≈10k
tokens, under what agent clients warn about): lists show as many lines as fit and say where to continue (page /
offset / part). Media are linked on DTF's CDN, links point to dtf.ru.
"""

from __future__ import annotations

import datetime as _dt
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
from ..history import diff_runs, split_lead, summary
from ..normalize import Linker, MediaResolver, comment_text
from ..reactions import Reactions
from ..search.engine import Match, has_index, other_forms, prepare, search
from ..state import Archive, archive_dirs, unpack
from ..util import MSK, ts_human
from ..viewdb import open_view, view_meta

MAX_CHARS = 30_000
TAG_RE = re.compile(r"<[^>]+>")
EVENTS = {"edit": "изменён", "removed": "удалён", "restored": "снова есть на DTF"}


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


def _fit(lines: list[str], used: int = 0) -> int:
    """How many of `lines` fit into an answer next to `used` characters (room is left for a footer; at least one)."""
    room, n = MAX_CHARS - used - 300, 0
    for ln in lines:
        room -= len(ln) + 1
        if room < 0:
            break
        n += 1
    return max(n, 1) if lines else 0


def _parts(text: str, size: int) -> list[str]:
    """`text` in pieces of at most `size` characters, cut at paragraph breaks (an endless paragraph is cut as is)."""
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        while len(para) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:size])
            para = para[size:]
        if cur and len(cur) + 2 + len(para) > size:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    return parts + [cur] if cur or not parts else parts


def _entries(db: sqlite3.Connection, eids: Any) -> dict[int, tuple]:
    """post id -> (title, subsite_id, subsite_name, own) of the posts the archive knows (its own and commented ones)."""
    ids = list({e for e in eids if e})
    out: dict[int, tuple] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update({r[0]: tuple(r[1:]) for r in db.execute(
            f"SELECT id, title, subsite_id, subsite_name, own FROM entries WHERE id IN ({','.join('?' * len(chunk))})",
            chunk)})
    return out


def _period(year: Any, date_from: Any, date_to: Any) -> tuple[int | None, int | None]:
    """[from, to) in unix time: dates are whole Moscow days, `year` fills what the dates leave open."""
    a, b = _date(date_from), _date(date_to, end=True)
    if year:
        a = a or _date(f"{int(year)}-01-01")
        b = b or _date(f"{int(year) + 1}-01-01")
    return a, b


WHERE = {"all": "", "posts": "p", "my_comments": "c", "others_comments": "o"}   # search: kind of the hits
# count: what to count -> (FROM with alias d, condition, post id column, author column)
SOURCES = {"my_comments": ("comments d", "d.mine=1", "d.entry_id", "d.author"),
           "post_comments": ("comments d", "d.own_post=1", "d.entry_id", "d.author"),
           "posts": ("posts d", "1", "d.id", "NULL")}
GROUPS = ("none", "post", "post_author", "author", "year", "month", "day")


def _diff_text(a: str, b: str, keep: int = 160) -> str:
    """What changed between two texts: [-removed-] {+added+}, long unchanged stretches shortened to their ends."""
    out: list[str] = []
    for op, x, y in diff_runs(a, b):
        if op == "equal":
            out.append(y if len(y) <= 2 * keep else y[:keep] + " … " + y[-keep:])
            continue
        lead, x, y = split_lead(x, y)
        out.append(lead + (f"[-{x}-]" if x.strip() else "") + (f"{{+{y}+}}" if y.strip() else ""))
    return "".join(out)


class _View:
    """What a tool needs of one archive: its view.sqlite (opened per call: a rebuild must be able to replace the file)
    and the names of its people."""

    def __init__(self, arch: Archive, rx: Reactions):
        self.arch = arch
        db = open_view(arch)
        if db is None:
            raise ToolError(f"архив @{arch.nick} сейчас пересобирается — повторите через пару минут"
                            if arch.view_path.exists() else
                            f"архив @{arch.nick} ещё не собран — дождитесь окончания первой синхронизации")
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

    def post_ref(self, eid: int | None, e: tuple | None, owners: dict[int, str]) -> str:
        """«Title» (post_id N, автор: X) — whose post it is; `e` from _entries, `owners` from Tools.owners."""
        if not eid:
            return "(без поста)"
        if e is None:
            return f"(post_id {eid}, название не сохранено)"
        title, sid, sname, own = e
        head = f"«{title or 'Без заголовка'}» (post_id {eid}"
        if own or sid == self.uid:
            return head + (", пост автора архива)" if eid in self.local_posts else ", пост автора архива, в архиве его нет)")
        # subsite_name: the author's name when the owner's comment was saved; users may know a newer one
        now = (self.users.get(sid) or {}).get("name")
        name = sname or now or (f"id{sid}" if sid else "неизвестен")
        if now and sname and now != sname:
            name += f", сейчас: {now}"
        return head + f", автор: {name}" + (f", пост архива @{owners[sid]}" if sid in owners else "") + ")"

    def comment_line(self, c: dict, depth: int = 0, width: int = 0) -> str:
        text = comment_text(c.get("text")).text.strip().replace("\n", " ")
        if width and len(text) > width:
            text = text[:width].rstrip() + "…"
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

    def owners(self, nick: str) -> dict[int, str]:
        """DTF uid -> nick of the library's other archives: their owners' posts are not just "someone else's"."""
        out: dict[int, str] = {}
        for n in self.nicks():
            if n != nick:
                try:
                    out[self.view(n).uid] = n
                except ToolError:
                    pass
        return out

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
            line = f"- **@{nick}** — {prof.get('name') or nick} (DTF id {v.uid}): постов {c.get('posts', 0)}"
            if v.meta.get("comments", True):
                try:
                    db = v.db()
                    try:
                        mine = db.execute("SELECT COUNT(*) FROM comments WHERE mine=1").fetchone()[0]
                    finally:
                        db.close()
                except ToolError:               # being rebuilt right now: the counts from its meta still do
                    mine = 0
                extra = mine - c.get("my_comments", 0)
                line += (f", своих комментариев {c.get('my_comments', 0)} в ленте DTF"
                         + (f" (+{extra} сохранены из веток: удалённые модератором и т. п.)" if extra > 0 else "")
                         + f", комментариев под постами {c.get('post_comments', 0)} (с удалёнными)")
            else:
                line += ", только посты"
            rows.append(line + f"; собран {built}")
        if not rows:
            return "Архивов пока нет."
        return "Архивы LDTF:\n" + "\n".join(rows)

    def search(self, query: str, archive: str | None = None, where: str = "all", year: int | None = None,
               date_from: str | None = None, date_to: str | None = None, sort: str = "relevance", limit: int = 20,
               page: int = 1, exact: bool = False) -> str:
        v = self.view(archive)
        kind = WHERE.get(where)
        if kind is None:
            raise ToolError("where: all | posts | my_comments | others_comments")
        a, b = _period(year, date_from, date_to)
        limit = max(1, min(int(limit or 20), 50))
        page = max(1, int(page or 1))
        db = v.db()
        try:
            if not has_index(db):
                raise ToolError("у архива нет поискового индекса — пересоберите его в LDTF")
            res = search(db, query, kind, a, b, {"relevance": "rank", "newest": "date", "oldest": "old"}.get(sort, "rank"),
                         page, limit, bool(exact))
            if res.error:
                raise ToolError(res.error)
            ids = [h.ref for h in res.hits if h.kind != "p"]
            parents: dict[int, dict] = {}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in db.execute(f"SELECT id, entry_id, data FROM comments WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                    parents[r[0]] = {"entry": r[1], **unpack(r[2])}
            ents = _entries(db, (h.entry for h in res.hits))
            forms = other_forms(db, res.query) if page == 1 else []
        finally:
            db.close()
        owners = self.owners(v.arch.nick)
        out = [f"Найдено: {res.total} (страница {page}, по {limit})"
               + (f"; исправлено: {'; '.join(res.notes)}; без исправлений: exact=true" if res.notes else "")]
        if res.suggestion:
            out.append(f"Возможно, имелось в виду: {res.suggestion}")
        if forms:
            out.append("Другие слова в архиве, которые запрос не находит (если подходят — добавьте через OR): "
                       + ", ".join(f"{t} ({n})" for t, n in forms))
        for h in res.hits:
            snip = _plain(res.marker.snippet(h.body, 300)[0]) if res.marker else h.body[:300]
            if h.kind == "p":
                out.append(f"\n### Пост {h.ref}: {h.title}\n{ts_human(h.date)} · {post_url(h.ref)}\n{snip}")
            else:
                c = parents.get(h.ref) or {}
                who = "автор архива" if h.kind == "c" else v.who(h.author)
                out.append(f"\n### Комментарий {h.ref} ({who})\n{ts_human(h.date)} · в посте "
                           f"{v.post_ref(h.entry, ents.get(h.entry), owners)} · {comment_url(h.entry, h.ref) if h.entry else ''}"
                           + (f" · в ответ на комментарий {c['replyTo']}" if c.get("replyTo") else "") + f"\n{snip}")
        if res.total > page * limit:
            out.append(f"\nЕщё результаты: page={page + 1}")
        return _cut("\n".join(out), "сузьте запрос или уменьшите limit")

    def count(self, archive: str | None = None, source: str = "my_comments", query: str | None = None,
              where: str = "all", exact: bool = False, group_by: str = "none", posts: str = "all",
              year: int | None = None, date_from: str | None = None, date_to: str | None = None, limit: int = 20,
              offset: int = 0) -> str:
        v = self.view(archive)
        if source not in SOURCES:
            raise ToolError("source: my_comments | post_comments | posts")
        if group_by not in GROUPS:
            raise ToolError("group_by: " + " | ".join(GROUPS))
        if posts not in ("all", "own", "foreign"):
            raise ToolError("posts: all | own | foreign")
        if query is None and where != "all":
            raise ToolError("where — только вместе с query (что искать); без query считает source")
        if query is not None and source != "my_comments":
            raise ToolError("с query считаются попадания поиска, source не нужен — где искать, задаёт where")
        if query is None and source == "posts" and (group_by in ("post", "post_author", "author") or posts != "all"):
            raise ToolError("посты в архиве — только посты автора: группируйте их по year / month / day")
        if query is None and source == "my_comments" and group_by == "author":
            raise ToolError("у своих комментариев один автор; по авторам постов — group_by=post_author")
        if query is None and source != "posts" and not v.meta.get("comments", True):
            raise ToolError(f"архив @{v.arch.nick} — «только посты», комментариев в нём нет")
        a, b = _period(year, date_from, date_to)
        args: dict[str, Any] = {"uid": v.uid}
        db = v.db()
        try:
            if query is not None:
                kind = WHERE.get(where)
                if kind is None:
                    raise ToolError("where: all | posts | my_comments | others_comments")
                if not has_index(db):
                    raise ToolError("у архива нет поискового индекса — пересоберите его в LDTF")
                m = prepare(db, query, kind, a, b, bool(exact))
                if m.res.error:
                    raise ToolError(m.res.error)
                frm, cond, entry, author, date = Match.FROM, m.where or "0", "d.entry", "d.author", "d.date"
                args.update(m.args)
                notes = m.res.notes
            else:
                frm, cond, entry, author = SOURCES[source]
                date = "d.date"
                notes = []
                if a is not None:
                    cond += f" AND {date} >= :a"
                    args["a"] = a
                if b is not None:
                    cond += f" AND {date} < :b"
                    args["b"] = b
            if posts == "own":
                cond += " AND (COALESCE(e.own, 0) = 1 OR e.subsite_id = :uid)"
            elif posts == "foreign":
                cond += " AND COALESCE(e.own, 0) = 0 AND COALESCE(e.subsite_id, 0) <> :uid"
            # post_author: by name — subsite_id is a community for older posts (many authors), not the author
            key = {"none": "''", "post": entry, "post_author": "e.subsite_name", "author": author,
                   "year": f"strftime('%Y', {date}, 'unixepoch', '+3 hours')",
                   "month": f"strftime('%Y-%m', {date}, 'unixepoch', '+3 hours')",
                   "day": f"strftime('%Y-%m-%d', {date}, 'unixepoch', '+3 hours')"}[group_by]
            rows = db.execute(f"SELECT {key} AS g, COUNT(*), MIN({date}), MAX({date}), MAX(e.title), MAX(e.subsite_id), "
                              f"MAX(e.subsite_name), MAX(e.own), MIN(e.subsite_id) FROM {frm} "
                              f"LEFT JOIN entries e ON e.id = {entry} "
                              f"WHERE {cond} GROUP BY g", args).fetchall()
        except sqlite3.OperationalError as e:
            raise ToolError(f"не удалось посчитать: {e}") from None
        finally:
            db.close()
        total = sum(r[1] for r in rows)
        what = (f"Найдено по запросу «{query}»" if query is not None else
                {"my_comments": "Комментариев автора", "post_comments": "Комментариев под постами автора",
                 "posts": "Постов автора"}[source])
        span = (f" ({ts_human(min(r[2] for r in rows))} — {ts_human(max(r[3] for r in rows))})" if total else "")
        head = f"{what}: {total}{span}" + ({"own": ", под своими постами", "foreign": ", под чужими постами"}.get(posts, ""))
        if notes:
            head += f"; исправлено: {'; '.join(notes)}; без исправлений: exact=true"
        if group_by == "none" or not total:
            return head
        if group_by in ("year", "month", "day"):
            rows.sort(key=lambda r: r[0] or "")
        else:
            rows.sort(key=lambda r: (-r[1], str(r[0])))
        owners = self.owners(v.arch.nick) if group_by in ("post", "post_author") else {}
        names = {(self.view(n).meta.get("profile") or {}).get("name"): n for n in owners.values()}
        limit = max(1, min(int(limit or 20), 500))
        offset = max(0, int(offset or 0))
        lines = []
        for g, n, _, _, title, sid, sname, own, sid_min in rows[offset:offset + limit]:
            if group_by == "post":
                lines.append(f"- **{n}** · {v.post_ref(g, (title, sid, sname, own) if sid is not None or title else None, owners)}")
            elif group_by == "post_author":
                one = sid if sid == sid_min else None      # one blog; old DTF communities mix many authors
                other = owners.get(sid) or owners.get(sid_min) or names.get(g)
                tag = ", автор архива" if own or v.uid in (sid, sid_min) else f", архив @{other}" if other else ""
                lines.append(f"- **{n}** · {g or 'пост не сохранён'}{tag}" + (f" (subsite_id {one})" if one else ""))
            elif group_by == "author":
                lines.append(f"- **{n}** · {v.who(g)} (id {g})")
            else:
                lines.append(f"- {g}: {n}")
        shown = _fit(lines, len(head) + 200)
        lines = lines[:shown]
        label = {"post": "по постам", "post_author": "по авторам постов", "author": "по авторам комментариев",
                 "year": "по годам", "month": "по месяцам", "day": "по дням"}[group_by]
        out = [head + f"; {label} — групп {len(rows)}, показаны {offset + 1}–{offset + shown}"]
        if group_by in ("year", "month", "day"):
            top = max(rows, key=lambda r: r[1])
            out[0] += f"; больше всего: {top[0]} ({top[1]})"
        out += lines
        if offset + shown < len(rows):
            out.append(f"\nЕщё: offset={offset + shown}")
        if group_by == "post" and query is None and source == "my_comments":
            out.append("Свои комментарии к посту: list_comments post_id=…")
        elif group_by == "post" and query is None and source == "post_comments":
            out.append("Комментарии к посту: get_post post_id=… comments=true")
        return "\n".join(out)

    def get_post(self, post_id: int, archive: str | None = None, version: int | None = None, comments: bool = False,
                 comments_limit: int = 200, comments_offset: int = 0, part: int = 1) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            row = db.execute("SELECT * FROM posts WHERE id=?", (int(post_id),)).fetchone()
            if row is None:
                raise ToolError(self._not_here(v, int(post_id), _entries(db, [int(post_id)]).get(int(post_id))))
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
        off = max(0, int(comments_offset or 0))
        pieces = _parts(md, MAX_CHARS - 3000)
        n_parts = len(pieces)
        part = max(1, int(part or 1))
        if part > n_parts:
            raise ToolError(f"у поста {n_parts} {'часть' if n_parts == 1 else 'части' if n_parts < 5 else 'частей'}")
        last = part == n_parts
        if comments and off:            # the next page of comments: no need to repeat the text
            body = "(текст поста — в ответе без comments_offset)"
        else:
            body = (f"Часть {part} из {n_parts}\n\n" if n_parts > 1 else "") + pieces[part - 1]
        out = [f"# {p.get('title') or 'Без заголовка'}", note + " · ".join(meta), "", body]
        rd = (p.get("repostData") or {}).get("data")
        if rd:
            out.insert(2, f"Репост: «{rd.get('title') or ''}» {rd.get('url') or ''}")
        if not last and not (comments and off):
            out.append(f"\nПродолжение: part={part + 1} (из {n_parts})"
                       + (f"; комментарии — с последней частью (part={n_parts})" if comments else ""))
        elif comments:
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
            lim = max(1, min(int(comments_limit or 200), 1000))
            chunk = lines[off:off + lim]
            shown = _fit(chunk, len("\n".join(out)) + 100)
            chunk = chunk[:shown]
            out += ["", f"## Комментарии ({len(items)}; показаны {off + 1}–{off + len(chunk)})"] + chunk
            if off + len(chunk) < len(lines):
                out.append(f"\nЕщё: comments_offset={off + len(chunk)}")
        return _cut("\n".join(out), "комментарии — по частям: comments_offset / comments_limit")

    def _not_here(self, v: _View, pid: int, e: tuple | None) -> str:
        """Why get_post has no such post, and where to look instead."""
        for n in self.nicks():
            if n != v.arch.nick:
                try:
                    if pid in self.view(n).local_posts:
                        return f"поста {pid} нет в архиве @{v.arch.nick}, он есть в архиве @{n}: get_post archive={n}"
                except ToolError:
                    pass
        if e is not None:
            return (f"поста {pid} в архиве @{v.arch.nick} нет — это {v.post_ref(pid, e, self.owners(v.arch.nick))}; "
                    f"комментарии автора архива к нему — list_comments post_id={pid}, ветки — get_comment")
        return (f"поста {pid} в архиве @{v.arch.nick} нет (это посты автора архива; чужие посты — "
                f"только комментарии в get_comment)")

    def get_comment(self, comment_id: int, archive: str | None = None, context: bool = True) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            row = db.execute("SELECT entry_id, data FROM comments WHERE id=?", (int(comment_id),)).fetchone()
            if row is None:
                raise ToolError(f"комментария {comment_id} в архиве @{v.arch.nick} нет")
            eid = row["entry_id"]
            items = [unpack(r[0]) for r in db.execute("SELECT data FROM comments WHERE entry_id=?", (eid,))] if context else []
            ent = _entries(db, [eid]).get(eid)
        finally:
            db.close()
        c = unpack(row["data"])
        out = [f"Комментарий {comment_id} в посте {v.post_ref(eid, ent, self.owners(v.arch.nick))} · "
               f"{comment_url(eid, int(comment_id))}"]
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
        lines = []
        for r in rows[offset:offset + limit]:
            extra = [f"комм. {r['comments']}", f"▲{r['pos']} ▼{r['neg']}"]
            if r["donations"]:
                extra.append(f"донаты {r['donations']} ₽")
            if r["versions"] and r["versions"] > 1:
                extra.append(f"версий {r['versions']}")
            if r["site"]:
                extra.append(state_title(r["site"]))
            lines.append(f"- {ts_human(r['date'])} · id {r['id']} · **{r['title'] or 'Без заголовка'}** · {', '.join(extra)}")
        shown = _fit(lines, 200)
        out = [f"Постов: {len(rows)}; показаны {offset + 1}–{offset + shown} (сортировка {sort}; комм. — по счётчику DTF)"]
        out += lines[:shown]
        if offset + shown < len(rows):
            out.append(f"\nЕщё: offset={offset + shown}")
        return "\n".join(out)

    def list_comments(self, archive: str | None = None, sort: str = "new", date_from: str | None = None,
                      date_to: str | None = None, post_id: int | None = None, limit: int = 50, offset: int = 0) -> str:
        v = self.view(archive)
        a, b = _date(date_from), _date(date_to, end=True)
        where, args = ["mine=1"], []
        if a is not None:
            where.append("date >= ?")
            args.append(a)
        if b is not None:
            where.append("date < ?")
            args.append(b)
        if post_id is not None:
            where.append("entry_id = ?")
            args.append(int(post_id))
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        db = v.db()
        try:
            total = db.execute(f"SELECT COUNT(*) FROM comments WHERE {' AND '.join(where)}", args).fetchone()[0]
            rows = db.execute(f"SELECT id, entry_id, data FROM comments WHERE {' AND '.join(where)} ORDER BY date "
                              f"{'ASC' if sort == 'old' else 'DESC'} LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
            ents = _entries(db, (r[1] for r in rows))
        finally:
            db.close()
        owners = self.owners(v.arch.nick)
        lines, cut = [], False
        for r in rows:
            c = unpack(r[2])
            line = v.comment_line(c, width=300)
            cut = cut or len(comment_text(c.get("text")).text.strip()) > 300
            lines.append(line + f" — в посте {v.post_ref(r[1], ents.get(r[1]), owners)}")
        shown = _fit(lines, 300)
        head = f"Комментариев автора: {total}" + (f" в посте {post_id}" if post_id is not None else "")
        out = [head + (f"; показаны {offset + 1}–{offset + shown}" if total else "")] + lines[:shown]
        if offset + shown < total:
            out.append(f"\nЕщё: offset={offset + shown}")
        if cut:
            out.append("Длинные тексты обрезаны до 300 символов; полностью — get_comment")
        return "\n".join(out)

    def get_history(self, kind: str | None = None, id: int | None = None, archive: str | None = None,  # noqa: A002
                    event: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    limit: int = 50, offset: int = 0) -> str:
        if kind not in (None, "", "post", "comment"):
            raise ToolError("kind: post | comment")
        if event not in (None, "", *EVENTS):
            raise ToolError("event: edit | removed | restored")
        v = self.view(archive)
        if id is None:
            return self._changes(v, kind, event, date_from, date_to, limit, offset)
        if not kind:
            raise ToolError("с id нужен kind: post | comment (без id — лента всех изменений)")
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
            """A post as Markdown (links, cards and media show in the diff too), a comment as its text."""
            if kind == "post":
                return x.get("title", "") + "\n" + render_blocks(x.get("blocks") or [], v.ctx)[1]
            return comment_text(x.get("text")).text

        def what_changed(a: dict, b: dict) -> str:
            """The blocks of a post or the attachments of a comment, as a line before the text diff."""
            if kind == "post":
                return summary(a.get("blocks") or [], b.get("blocks") or [], a.get("title", "") != b.get("title", ""))
            na, nb = len(a.get("media") or []), len(b.get("media") or [])
            return f"вложений было {na}, стало {nb}" if na != nb else ""
        versions = [unpack(r["body"]) for r in rows if r["event"] == "edit"]
        chain = versions + ([cur] if cur else [])
        texts = [text_of(x) for x in chain]
        out = [f"История {'поста' if kind == 'post' else 'комментария'} {id}" + (f" (с {ts_human(since)})" if since else "")]
        i = 0
        for r in rows:
            if r["event"] == "edit":
                head = what_changed(chain[i], chain[i + 1]) if i + 1 < len(chain) else ""
                out.append(f"\n## Версия {r['id']} от {ts_human(r['version_date'])} — заменена правкой "
                           f"({ts_human(r['at'])})\nЧто изменилось в следующей версии"
                           + (f" ({head})" if head else "") + " ([-удалено-] {+добавлено+}):\n"
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

    def _changes(self, v: _View, kind: str | None, event: str | None, date_from: str | None, date_to: str | None,
                 limit: int, offset: int) -> str:
        """get_history without id: every edit, removal and return the archive saw, newest first."""
        where, args = ["1"], []
        if kind:
            where.append("h.kind=?")
            args.append(kind)
        if event:
            where.append("h.event=?")
            args.append(event)
        a, b = _date(date_from), _date(date_to, end=True)
        if a is not None:
            where.append("h.at >= ?")
            args.append(a)
        if b is not None:
            where.append("h.at < ?")
            args.append(b)
        w = " AND ".join(where)
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        db = v.db()
        try:
            counts = dict(db.execute(f"SELECT h.event, COUNT(*) FROM history h WHERE {w} GROUP BY h.event", args).fetchall())
            rows = db.execute(f"SELECT h.id, h.kind, h.item_id, COALESCE(h.entry_id, c.entry_id), h.at, h.event, h.state, "
                              f"h.version_date, c.data FROM history h LEFT JOIN comments c ON h.kind='comment' AND "
                              f"c.id=h.item_id WHERE {w} ORDER BY h.at DESC, h.id DESC LIMIT ? OFFSET ?",
                              args + [limit, offset]).fetchall()
            ents = _entries(db, (r[2] if r[1] == "post" else r[3] for r in rows))
        finally:
            db.close()
        since = v.meta.get("history_since")
        total = sum(counts.values())
        if not total:
            return "Изменений на DTF не найдено" + (f" (LDTF следит за ними с {ts_human(since)})" if since else "") + "."
        owners = self.owners(v.arch.nick)
        lines = []
        for hid, k, item, eid, at, ev, state, vdate, data in rows:
            what = EVENTS.get(ev, ev)
            if ev == "edit":
                what += f" (версия {hid} от {ts_human(vdate)})"
            elif ev == "removed" and state:
                what += f": {state_title(state)}"
            if k == "post":
                lines.append(f"- {ts_human(at)} · пост {v.post_ref(item, ents.get(item), owners)} · {what}")
            else:
                c = unpack(data) if data else {}
                text = comment_text(c.get("text")).text.strip().replace("\n", " ")
                lines.append(f"- {ts_human(at)} · комментарий {item} ({v.who(c.get('author'))}) в посте "
                             f"{v.post_ref(eid, ents.get(eid), owners)} · {what}"
                             + (f" · «{text[:120]}{'…' if len(text) > 120 else ''}»" if text else ""))
        shown = _fit(lines, 400)
        out = [f"Изменения на DTF: {total} (правок {counts.get('edit', 0)}, удалений {counts.get('removed', 0)}, "
               f"возвращений {counts.get('restored', 0)})" + (f", LDTF следит за ними с {ts_human(since)}" if since else "")
               + f"; показаны {offset + 1}–{offset + shown}, сначала новые"] + lines[:shown]
        if offset + shown < total:
            out.append(f"\nЕщё: offset={offset + shown}")
        out.append("Что именно изменилось: get_history kind=… id=…")
        return "\n".join(out)

    def archive_stats(self, archive: str | None = None) -> str:
        v = self.view(archive)
        db = v.db()
        try:
            one = lambda q, *a: db.execute(q, a).fetchone()   # noqa: E731
            posts = one("SELECT COUNT(*), MIN(date), MAX(date), SUM(comments), SUM(donations) FROM posts")
            under = one("SELECT COUNT(*) FROM comments WHERE own_post=1")[0]
            mine = one("SELECT COUNT(*), MIN(date), MAX(date), SUM(feed) FROM comments WHERE mine=1")
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
               f"Постов: {posts[0]} ({ts_human(posts[1])} — {ts_human(posts[2])}), комментариев под ними: "
               f"по счётчикам DTF {posts[3] or 0}" + (f", в архиве {under} (вместе с удалёнными)" if under else "")]
        if mine[0]:
            extra = mine[0] - (mine[3] or 0)
            out.append(f"Комментариев автора: {mine[0]} ({ts_human(mine[1])} — {ts_human(mine[2])})"
                       + (f"; из них {extra} нет в ленте DTF (удалённые модератором и т. п., сохранены из веток)"
                          if extra > 0 else ""))
            out.append("По годам: " + ", ".join(f"{y}: {n}" for y, n in years))
        out.append("Посты по годам: " + ", ".join(f"{y}: {n}" for y, n in pyears))
        out.append("\n## Самые популярные посты (▲ реакции)")
        out += [f"- id {pid}: {t or 'Без заголовка'} — ▲{s[0]} ▼{s[1]}, комм. {n}" for s, pid, t, n in scored[:5]]
        if got or don[0]:
            out.append(f"\n## Донаты\nПостам: {got} ₽ (сумма DTF, у старых постов — по донат-комментариям); "
                       f"донат-комментариев: {don[0]} на {don[1]} ₽")
            out += [f"- id {pid}: {t or 'Без заголовка'} — {s} ₽" for pid, t, s in top_don]
            if donors:
                out.append("Донатеры: " + ", ".join(f"{v.who(a)} (id {a}) — {s} ₽ ({n})" for a, s, n in donors))
        if hist:
            out.append(f"\n## Изменения на DTF\nправок {hist.get('edit', 0)}, удалений {hist.get('removed', 0)}, "
                       f"возвращений {hist.get('restored', 0)} — список: get_history без id")
        out.append("\nПодсчёты по периодам, постам и авторам — count")
        return "\n".join(out)

# ---------------------------------------------------------------------- definitions (tools/list)
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


ARCHIVE = {"type": "string", "description": "Archive nickname (see list_archives). Optional when the library has one."}
DATE = {"type": "string", "description": "YYYY-MM-DD (Moscow time)"}
WHERE_ARG = {"type": "string", "enum": list(WHERE), "default": "all"}
EXACT = {"type": "boolean", "default": False, "description": "No typo / keyboard layout correction"}
TOOLS: list[dict] = [
    {"name": "list_archives", "title": "Архивы LDTF",
     "description": "List the archived DTF.ru profiles of this LDTF library: nickname, name, number of posts and "
                    "comments, when built. Call first to get the `archive` argument for other tools.",
     "inputSchema": _schema({})},
    {"name": "search", "title": "Поиск",
     "description": "Full-text search over an archive's posts and comments (Russian morphology, typo and keyboard "
                    "layout correction). Query syntax: words (all forms), \"exact phrase\", -exclude, a OR b, prefix* "
                    "(a prefix is taken literally, never corrected). Diminutives and derived words have stems of their "
                    "own: the answer lists such words the query misses — add them with OR or use a shorter prefix. "
                    "Returns hits with ids, dates, DTF links, the post and its author, and snippets. To count hits "
                    "(by year, post…) use count instead of paging.",
     "inputSchema": _schema({"query": {"type": "string", "description": "What to find"}, "archive": ARCHIVE,
                             "where": WHERE_ARG, "year": {"type": "integer"}, "date_from": DATE, "date_to": DATE,
                             "sort": {"type": "string", "enum": ["relevance", "newest", "oldest"], "default": "relevance"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                             "page": {"type": "integer", "minimum": 1, "default": 1},
                             "exact": EXACT}, ["query"])},
    {"name": "count", "title": "Подсчёт",
     "description": "Counts instead of lists — use it for how many / when / under which post / who most questions "
                    "rather than paging through list_comments or search. Counts the owner's comments (source="
                    "my_comments, default), comments under the owner's posts (post_comments) or the owner's posts "
                    "(posts); with `query` — search hits (where as in search; a post hit groups under itself). "
                    "group_by: none | post | post_author (whose posts) | author (who wrote the comments; not for "
                    "my_comments) | year | month | day (Moscow time). posts: all | own | foreign (under whose posts).",
     "inputSchema": _schema({"archive": ARCHIVE,
                             "source": {"type": "string", "enum": list(SOURCES), "default": "my_comments"},
                             "query": {"type": "string", "description": "Count search hits for this query"},
                             "where": WHERE_ARG, "exact": EXACT,
                             "group_by": {"type": "string", "enum": list(GROUPS), "default": "none"},
                             "posts": {"type": "string", "enum": ["all", "own", "foreign"], "default": "all"},
                             "year": {"type": "integer"}, "date_from": DATE, "date_to": DATE,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20,
                                       "description": "Groups to show"},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
    {"name": "get_post", "title": "Пост",
     "description": "One post of the archive owner as Markdown: text, media links, reactions, donations, optionally "
                    "its comment tree. `version`: an earlier version id from get_history. A long post comes in parts "
                    "(`part`); with comments_offset > 0 only the next comments are returned, without the text. Someone "
                    "else's post is not archived — the error says where to look (another archive or list_comments).",
     "inputSchema": _schema({"post_id": {"type": "integer"}, "archive": ARCHIVE,
                             "version": {"type": "integer", "description": "History version id (earlier text)"},
                             "comments": {"type": "boolean", "default": False},
                             "comments_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                             "comments_offset": {"type": "integer", "minimum": 0, "default": 0},
                             "part": {"type": "integer", "minimum": 1, "default": 1,
                                      "description": "Part of a long post"}}, ["post_id"])},
    {"name": "get_comment", "title": "Комментарий с контекстом",
     "description": "A comment with its thread: the comments above it and all replies below (as archived), with the "
                    "post it belongs to and the post's author.",
     "inputSchema": _schema({"comment_id": {"type": "integer"}, "archive": ARCHIVE,
                             "context": {"type": "boolean", "default": True}}, ["comment_id"])},
    {"name": "list_posts", "title": "Посты",
     "description": "The archive owner's posts with dates, comment counts, reactions (▲ positive / ▼ dislikes), "
                    "donations, number of versions; sortable and filterable by date.",
     "inputSchema": _schema({"archive": ARCHIVE,
                             "sort": {"type": "string", "enum": ["new", "old", "reactions", "comments", "donations"],
                                      "default": "new"},
                             "date_from": DATE, "date_to": DATE,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
    {"name": "list_comments", "title": "Комментарии автора",
     "description": "Comments written by the archive owner anywhere on DTF, newest or oldest first, with the post "
                    "each one is under and its author; `post_id` — only under one post. Long texts are shortened: "
                    "get_comment shows one in full with its thread. For totals use count.",
     "inputSchema": _schema({"archive": ARCHIVE, "sort": {"type": "string", "enum": ["new", "old"], "default": "new"},
                             "date_from": DATE, "date_to": DATE, "post_id": {"type": "integer"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
    {"name": "get_history", "title": "История правок",
     "description": "Edits, removals and returns seen by LDTF. Without `id`: the feed of all changes in the archive "
                    "(filters kind, event, dates), newest first. With `kind` + `id`: one post or comment, with what "
                    "changed between versions ([-removed-] {+added+}).",
     "inputSchema": _schema({"kind": {"type": "string", "enum": ["post", "comment"]}, "id": {"type": "integer"},
                             "archive": ARCHIVE, "event": {"type": "string", "enum": list(EVENTS)},
                             "date_from": DATE, "date_to": DATE,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                             "offset": {"type": "integer", "minimum": 0, "default": 0}})},
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
                "count for how many / when / where most (not paging through lists), list_posts / list_comments to "
                "browse, get_history for edits and removals (without id: all of them), archive_stats for an overview. "
                "Read-only: nothing here changes the archives or DTF.")
