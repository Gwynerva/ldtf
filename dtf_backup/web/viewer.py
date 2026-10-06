"""Archive viewer pages, rendered on demand from .state/view.sqlite."""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import sqlite3
import urllib.parse as up
from typing import Any

from ..api import SITE, comment_url, post_url
from ..blocks import Ctx, Report, media_stub, remote_attrs, render_blocks, src_attr
from ..context import ancestors, index_tree
from ..guard import state_title
from ..normalize import EXT_LINK, MediaResolver, comment_text, media_info, media_src, post_ref
from ..reactions import Reactions
from ..scope import comments_kept
from ..search.engine import has_index, search
from ..state import Archive, unpack
from ..util import COMMENTS, MSK, POSTS, count_label, human_bytes, num, plural, rub, ts_date, ts_human
from ..viewdb import group_view, open_view, view_meta
from .icons import icon
from .history_pages import changes_count
from .insights import donations_total
from .ui import (MONTHS_SHORT, CommentView, Links, badge, banner, btn, empty_state, fold, icon_btn,
                 mi, month_title, page_head, pager, pagination, sec_head, stat)

E = html.escape


class ArchiveView:
    """Per-archive rendering context; rebuilt by the app when view.sqlite or the reactions config change."""

    def __init__(self, arch: Archive):
        self.arch = arch
        self.nick = arch.nick
        self.links = Links(arch.nick)
        db = open_view(arch)
        if db is None:
            raise FileNotFoundError("view.sqlite ещё не собрана")
        meta = view_meta(db)
        self.meta = meta
        self.prof: dict = meta["profile"]
        self.uid: int = meta["uid"]
        self.local_posts = {r[0] for r in db.execute("SELECT id FROM posts")}
        counts = meta.get("counts") or {}
        # comment pages exist while the archive keeps comments (or still has some): not for an archive of posts only
        self.comments_on = comments_kept(arch.settings()) or bool(counts.get("my_comments") or counts.get("post_comments"))
        self.users = {r["id"]: dict(r) for r in db.execute("SELECT id, name, nickname, uri, avatar FROM users")}
        db.close()
        self.report = Report()
        self.resolver = MediaResolver.for_library(arch.library)
        self.rx = Reactions.for_archive(arch, self.resolver, self.report)
        self.cv = CommentView(self.resolver, self.local_posts, self.users, self.uid, self.report, self.links, self.rx)

    def db(self) -> sqlite3.Connection:
        db = open_view(self.arch)
        assert db is not None
        return db

    def ctx(self, owner: str, where: str) -> Ctx:
        return Ctx(self.resolver, self.cv.linker, "/", "/", owner, where, self.report, self.local_posts)

    def entry_index(self, db: sqlite3.Connection, eid: int, cache: dict) -> tuple[dict, dict] | None:
        if eid not in cache:
            items = [unpack(r[0]) for r in db.execute("SELECT data FROM comments WHERE entry_id=?", (eid,))]
            cache[eid] = index_tree(items) if items else None
        return cache[eid]


# ---------------------------------------------------------------------- cards and helpers
def post_card(v: ArchiveView, row: sqlite3.Row) -> str:
    cov = json.loads(row["cover"]) if row["cover"] else None
    img = ""
    if cov:
        a = src_attr(cov, E(media_src(cov, "/"))) + remote_attrs(cov)
        stub = media_stub(cov["kind"], "gone", action=False) if cov.get("gone") and not cov.get("local") else ""
        img = (f'<video{a} muted loop playsinline preload="metadata" data-autoplay></video>'
               if cov["kind"] == "video" else f'<img loading="lazy"{a} alt="">') + stub
    pairs = [tuple(x) for x in json.loads(row["rx"] or "[]")]
    pos, _ = v.rx.split(pairs)
    rep = f'<span title="Репост">{icon("repeat")}</span>' if row["repost"] else ""
    site = row["site"] if "site" in row.keys() else None
    if site:
        rep += badge(state_title(site), "history", "site pc-site")
    lead = f'<div class="pc-l">{E(row["lead"])}</div>' if row["lead"] else ""
    don = row["donations"] or 0
    don_h = f'<span class="mi don" title="Донаты">{icon("volunteer_activism")}{rub(don)}</span>' if don else ""
    return (f'<a class="pcard card" href="{E(v.links.post(row["id"]))}" data-date="{row["date"]}" '
            f'data-comments="{row["comments"]}" data-reactions="{pos}" data-donations="{don}">'
            f'<div class="pc-img">{img}</div><div class="pc-b"><div class="pc-t">{rep}{E(row["title"] or "Без заголовка")}</div>'
            f'{lead}<div class="pc-m meta"><span>{ts_date(row["date"])}</span>'
            f'{mi("chat_bubble", num(row["comments"]), "Комментарии")}{v.rx.score_html(pairs)}{don_h}</div></div></a>')


def _entry_link(v: ArchiveView, eid: int | None, cid: int | None = None) -> tuple[str, str]:
    """(href, extra attrs) for a post: local page if it's one of the owner's posts, else dtf.ru."""
    if eid and eid in v.local_posts:
        return v.links.post(eid) + (f"#c{cid}" if cid else ""), ""
    if eid:
        return (comment_url(eid, cid) if cid else post_url(eid)), EXT_LINK
    return "#", ""


def _entry_head(v: ArchiveView, eid: int | None, title: str, sub: str | None, own: bool, badge: str = "",
                cls: str = "mc-post") -> str:
    """Header line of a discussion card: the post it belongs to (local page or dtf.ru)."""
    href, ext = _entry_link(v, eid)
    ic = icon("article") if own else icon("open_in_new")
    subh = f' <span class="muted">· {E(sub)}</span>' if sub and not own else ""
    return f'<div class="{cls}">{ic}<span class="eh-t"><a href="{E(href)}"{ext}>{E(title)}</a>{subh}</span>{badge}</div>'


# ---------------------------------------------------------------------- pages
def page_home(v: ArchiveView) -> tuple[str, str]:
    db = v.db()
    prof = v.prof
    cov = media_info(prof["cover"], v.resolver) if prof.get("cover") else None
    cover = ""   # over the cover's tint: if the picture fails to load, the tint stays
    if cov and cov["kind"] == "image" and not cov["gone"]:
        cover = f'<img class="cover-img" src="{E(media_src(cov, "/"))}" alt=""{remote_attrs(cov)}>'
    av = '<span class="ava av0">' + icon("person") + "</span>"
    m = media_info(prof["avatar"], v.resolver) if prof.get("avatar") else None
    if m and not m["gone"]:
        a = f' src="{E(media_src(m, "/"))}"' + remote_attrs(m)
        av = (f'<video class="ava"{a} muted loop autoplay playsinline></video>' if m["kind"] == "video"
              else f'<img class="ava"{a} alt="">')
    counts = v.meta["counts"]
    years: dict[str, int] = {}
    for r in db.execute("SELECT ym, comments FROM months ORDER BY ym DESC"):
        years[r["ym"][:4]] = years.get(r["ym"][:4], 0) + r["comments"]
    recent = "".join(post_card(v, r) for r in db.execute("SELECT * FROM posts ORDER BY date DESC LIMIT 9"))
    db.close()
    media_n, media_size = _media_usage(v)
    name = prof.get("name") or v.nick
    ext = icon_btn("open_in_new", "Профиль на DTF", prof.get("url") or SITE, attrs=EXT_LINK)
    desc = (prof.get("description") or "").strip()
    donated = donations_total(v)
    changed = changes_count(v)
    stats = (stat(num(counts["posts"]), plural(counts["posts"], *POSTS), v.links.posts()) +
             (stat(num(counts["my_comments"]), plural(counts["my_comments"], *COMMENTS), v.links.comments())
              if v.comments_on else "") +
             (stat(rub(donated), "донаты", v.links.donations()) if donated else "") +
             (stat(num(changed), plural(changed, "изменение", "изменения", "изменений") + " на DTF", v.links.changes())
              if changed else "") +
             (stat(num(media_n), f"медиа · {human_bytes(media_size)}") if media_n else ""))
    more = lambda label, href: f'<a class="btn text" href="{E(href)}">{E(label)}{icon("chevron_right")}</a>'  # noqa: E731
    yl = "".join(f'<a class="chip" href="{E(v.links.comments())}#y{y}">{y}<span class="n">{num(n)}</span></a>'
                 for y, n in years.items())
    body = (f'<section class="profile card"><div class="cover">{cover}</div>'
            f'<div class="pbody">{av}<div class="pinfo"><h1 class="pname">{E(name)}</h1>'
            f'<div class="psub">@{E(v.nick)} · на DTF с {ts_date(prof.get("created"))}</div></div>{ext}</div>'
            f'{f"<p class=pdesc>{E(desc)}</p>" if desc else ""}<div class="stats pstats">{stats}</div></section>'
            f'{sec_head("Последние посты", action=more("Все посты", v.links.posts()))}<div class="plist">{recent}</div>'
            + (f'{sec_head("Комментарии по годам", action=more("Календарь", v.links.comments()))}'
               f'<div class="chips">{yl}</div>' if yl and v.comments_on else ""))
    return name, body


def _media_usage(v: ArchiveView) -> tuple[int, int]:
    """Media files of the archive, via a short-lived connection (the server must not keep archive files open:
    Windows would refuse to delete an archive)."""
    if not v.arch.exists():
        return 0, 0
    a = Archive(v.arch.root, v.arch.library)
    try:
        return a.media_usage()
    finally:
        a.close()


def page_posts(v: ArchiveView) -> tuple[str, str]:
    db = v.db()
    rows = list(db.execute("SELECT * FROM posts ORDER BY date DESC"))
    db.close()
    by_year: dict[str, list] = {}
    for r in rows:
        by_year.setdefault(ts_date(r["date"])[-4:], []).append(r)
    years = "".join(f'<a class="chip" href="#y{y}">{y}<span class="n">{len(x)}</span></a>' for y, x in by_year.items())
    groups = "".join(f'<h2 class="year-h" id="y{y}">{y}</h2><div class="plist">{"".join(post_card(v, r) for r in x)}</div>'
                     for y, x in by_year.items())
    ck = icon("check", cls="ck")
    don = (f'<button data-sort="donations">{ck}Донаты</button>' if any(r["donations"] for r in rows) else "")
    sort = (f'<div class="seg sortbar" role="group" aria-label="Сортировка"><button class="on" data-sort="date">{ck}По дате</button>'
            f'<button data-sort="comments">{ck}Комментарии</button><button data-sort="reactions">{ck}Реакции</button>{don}</div>')
    body = (page_head("Посты", n=len(rows)) + f'<div class="toolbar">{sort}</div>'
            f'<div id="by-year"><div class="chips scroll years">{years}</div>{groups}</div>'
            f'<div id="flat" class="plist" hidden></div>')
    return "Посты", body


def _repost_html(v: ArchiveView, p: dict, ctx: Ctx) -> str:
    rd = (p.get("repostData") or {}).get("data")
    r = post_ref(rd)
    if r is None:
        return ""
    href, ext = _entry_link(v, r["id"]) if r["id"] else (r["url"], EXT_LINK)
    bh, _, _ = render_blocks(rd.get("blocks") or [], ctx)
    return (f'<div class="repost"><div class="meta">{mi("repeat", "Репост")}<span>{E(r["author"])}</span>'
            f'<span>{ts_human(r["date"])}</span></div>'
            f'<h2><a href="{E(href)}"{ext}>{E(r["title"] or "Пост")}</a></h2>{bh}</div>')


def page_post(v: ArchiveView, pid: int) -> tuple[str, str] | None:
    db = v.db()
    row = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if row is None:
        db.close()
        return None
    p = unpack(row["raw"])
    ctx = v.ctx(f"post:{pid}", f"post {pid}")
    blocks_h, _, _ = render_blocks(p.get("blocks") or [], ctx, ids=True)
    repost_h = _repost_html(v, p, ctx)
    items = [unpack(r[0]) for r in db.execute("SELECT data FROM comments WHERE entry_id=? AND own_post=1", (pid,))]
    by_id, children = index_tree(items)
    roots = [c["id"] for c in sorted(items, key=lambda c: (c["date"], c["id"])) if not c["replyTo"] or c["replyTo"] not in by_id]
    comments_h = v.cv.tree(roots, by_id, children, pid, anchors=True)
    newer = db.execute("SELECT id, title FROM posts WHERE date > ? ORDER BY date ASC LIMIT 1", (row["date"],)).fetchone()
    older = db.execute("SELECT id, title FROM posts WHERE date < ? ORDER BY date DESC LIMIT 1", (row["date"],)).fetchone()
    db.close()
    nav = pager((v.links.post(older["id"]), older["title"] or "Без заголовка") if older else None,
                (v.links.post(newer["id"]), newer["title"] or "Без заголовка") if newer else None,
                "Предыдущий пост", "Следующий пост")
    counters = p.get("counters") or {}
    pairs = [tuple(x) for x in json.loads(row["rx"] or "[]")]
    dm = p.get("dateModified")
    meta = [f'<span>{ts_human(p.get("date"))}</span>']
    versions, hist = row["versions"] or 1, row["hist"] or 0
    if versions > 1:   # the archive keeps earlier versions: their history
        meta.append(f'<a class="mi hist-link" href="{E(v.links.post_history(pid))}" title="Изменён {ts_human(dm)} — '
                    f'история правок">{icon("edit")}изменён · {count_label(versions, "версия", "версии", "версий")}</a>')
    elif dm and dm - (p.get("date") or 0) > 600:
        meta.append(mi("edit", "изменён", f"Изменён {ts_human(dm)}"))
    if row["source"] == "timeline":
        meta.append(badge("сохранён из ленты", "info", title="Страница поста была недоступна"))
    if row["unlisted"]:
        meta.append(badge("скрыт из профиля", "visibility_off"))
    site = (p.get("_site") or {}).get("state")
    if site:
        title = ("На DTF поста в прежнем виде больше нет; в архиве сохранена версия от "
                 f"{ts_human(p.get('dateModified') or p.get('date'))}")
        if hist:
            meta.append(f'<a class="badge site" href="{E(v.links.post_history(pid))}" title="{E(title)}">'
                        f'{icon("history")}{E(state_title(site))}</a>')
        else:
            meta.append(badge(state_title(site), "history", "site", title))
    meta.append('<span class="sp"></span>' + icon_btn("open_in_new", "Открыть на DTF", p.get("url") or post_url(pid),
                                                     attrs=EXT_LINK))
    foot = []
    if counters.get("favorites"):
        foot.append(f'<span class="cnt" title="В закладках">{icon("bookmark")}{num(counters["favorites"])}</span>')
    if counters.get("reposts"):
        foot.append(f'<span class="cnt" title="Репосты">{icon("repeat")}{num(counters["reposts"])}</span>')
    if row["donations"]:
        when = f" (данные DTF на {ts_human(row['stats_at'])})" if row["stats_at"] else ""
        foot.append(f'<a class="cnt don" href="{E(v.links.donations())}" title="Донаты{E(when)}">'
                    f'{icon("volunteer_activism")}{rub(row["donations"])}</a>')
    foot.append(v.rx.score_html(pairs) + v.rx.html(pairs, "/", f"post {pid}", limit=60))
    foot_h = "".join(foot)
    title = p.get("title") or ""
    if items or v.comments_on:
        comments = (f'<section class="comments card" id="comments">{sec_head("Комментарии", n=len(items))}'
                    f'{comments_h or "<p class=muted>Комментариев нет.</p>"}</section>')
    else:   # an archive of posts only: the discussion stays on DTF
        n = counters.get("comments") or 0
        link = (f' <a href="{E((p.get("url") or post_url(pid)) + "#comments")}"{EXT_LINK}>'
                f'{count_label(n, "комментарий", "комментария", "комментариев")} на DTF</a>' if n else "")
        comments = f'<p class="muted small no-comments">{icon("forum")}<span>Комментарии в этом архиве не сохраняются.{link}</span></p>'
    body = (f'<article class="post card" id="p{pid}"><div class="post-meta">{"".join(meta)}</div>'
            f'<h1>{E(title) or "<span class=muted>Без заголовка</span>"}</h1>{repost_h}{blocks_h}'
            f'{f"<div class=post-foot>{foot_h}</div>" if foot_h.strip() else ""}</article>{nav}{comments}')
    return title or f"Пост {pid}", body


def page_calendar(v: ArchiveView) -> tuple[str, str]:
    db = v.db()
    rows = list(db.execute("SELECT ym, comments FROM months ORDER BY ym DESC"))
    db.close()
    years: dict[str, dict[str, int]] = {}
    for r in rows:
        years.setdefault(r["ym"][:4], {})[r["ym"][5:]] = r["comments"]
    mx = max((r["comments"] for r in rows), default=1)
    out = []
    for y, months in years.items():
        cells = []
        for m in range(1, 13):
            key = f"{m:02d}"
            n = months.get(key)
            label = MONTHS_SHORT[m - 1].capitalize()
            if n:
                h = round(6 + 40 * n / mx)
                cells.append(f'<a href="{E(v.links.month(f"{y}-{key}"))}" style="--h:{h}%" '
                             f'title="{month_title(f"{y}-{key}")}: {num(n)}"><span class="m">{label}</span><b>{num(n)}</b></a>')
            else:
                cells.append(f'<span><span class="m">{label}</span></span>')
        total = sum(months.values())
        out.append(f'<section class="cal-year card" id="y{y}"><h2>{y}<span class="n">{num(total)} '
                   f'{plural(total, *COMMENTS)}</span></h2>'
                   f'<div class="cal-months">{"".join(cells)}</div></section>')
    total = sum(r["comments"] for r in rows)
    return "Комментарии", page_head("Комментарии", n=total) + "".join(out)


def page_month(v: ArchiveView, ym: str, page: int) -> tuple[str, str] | None:
    db = v.db()
    mrow = db.execute("SELECT * FROM months WHERE ym=?", (ym,)).fetchone()
    if mrow is None:
        db.close()
        return None
    page = max(1, min(page, mrow["pages"]))
    groups = list(db.execute("SELECT * FROM month_groups WHERE ym=? AND page=? ORDER BY pos", (ym, page)))
    cache: dict = {}
    arts = [_group_html(v, db, g, cache) for g in groups]
    months = [r[0] for r in db.execute("SELECT ym FROM months ORDER BY ym DESC")]
    db.close()
    i = months.index(ym)
    newer = months[i - 1] if i > 0 else None
    older = months[i + 1] if i + 1 < len(months) else None

    def nav_btn(target: str | None, ic: str) -> str:
        if not target:
            return f'<span class="icon-btn dis" aria-hidden="true">{icon(ic)}</span>'
        return icon_btn(ic, month_title(target), v.links.month(target))
    n, ng = mrow["comments"], mrow["groups"]
    sub = (f'{count_label(n, *COMMENTS)} · '
           f'{count_label(ng, "обсуждение", "обсуждения", "обсуждений")}')
    actions = (nav_btn(older, "chevron_left") + nav_btn(newer, "chevron_right") +
               btn("Все месяцы", "text", "calendar_month", href=v.links.comments()))
    ck = icon("check", cls="ck")
    filters = ('<div class="chips scroll filters">' + "".join(
        f'<label class="chip"><input type="checkbox" data-f="{k}">{ck}{t}</label>'
        for k, t in (("replies", "С ответами"), ("media", "С медиа"), ("own", "В своих постах"), ("foreign", "В чужих постах")))
        + "</div>")
    link = lambda k: v.links.month(ym, k)  # noqa: E731
    bottom = pager((v.links.month(older), month_title(older)) if older else None,
                   (v.links.month(newer), month_title(newer)) if newer else None, "Предыдущий месяц", "Следующий месяц")
    body = (page_head(month_title(ym), sub, actions) + filters + pagination(page, mrow["pages"], link, compact=True)
            + "".join(arts) + pagination(page, mrow["pages"], link) + bottom)
    return f"Комментарии: {month_title(ym)}", body


def _group_html(v: ArchiveView, db: sqlite3.Connection, g: sqlite3.Row, cache: dict) -> str:
    eid = g["entry_id"]
    mine = json.loads(g["mine"])
    erow = db.execute("SELECT title, subsite_name, own FROM entries WHERE id=?", (eid,)).fetchone() if eid else None
    title = (erow["title"] if erow else None) or ("Пост" if eid else "Пост недоступен")
    own = bool(g["own"])
    cnt = (badge(count_label(len(mine), *COMMENTS), cls="mc-n")
           if len(mine) > 1 else "")
    parts = [f'<article class="mc card" data-r="{g["has_replies"]}" data-m="{g["has_media"]}" data-o="{int(own)}">'
             + _entry_head(v, eid, title, erow["subsite_name"] if erow else None, own, cnt)]
    gv = group_view(v.entry_index(db, eid, cache) if eid else None, mine, g["root_id"])
    cv = v.cv
    if gv["standalone"]:
        for cid in mine:
            r = db.execute("SELECT data FROM comments WHERE id=?", (cid,)).fetchone()
            if r is None:
                continue
            c = unpack(r[0])
            parts.append(f'<div class="c-node">{cv.one(c, eid, True)}</div>')
            if (c["level"] > 0 or c["replyCount"] > 0) and eid:
                parts.append(f'<div class="ctx-note">{icon("info")}Контекст ветки не загружен · '
                             f'<a href="{E(comment_url(eid, cid))}"{EXT_LINK}>открыть на DTF</a></div>')
    else:
        by_id, chain = gv["by_id"], gv["chain"]
        if len(chain) > 1:
            flat = "".join(f'<div class="c-node ctx">{cv.one(by_id[a], eid, True)}</div>' for a in chain[:-1])
            n = len(chain) - 1
            parts.append(fold(f"Ещё {n} выше по ветке", flat, "ctx-more"))
        if chain:
            parts.append(f'<div class="c-node ctx">{cv.one(by_id[chain[-1]], eid, True)}</div>')
        keep = {k: by_id[k] for k in gv["keep"]}
        parts.append(cv.tree([gv["cur"]], keep, gv["kids"], eid, anchors=True))
    parts.append("</article>")
    return "".join(parts)


def page_go(v: ArchiveView, cid: int) -> str | None:
    """Where a comment is shown: month page of the owner's comment, the owner's post, or the group around it."""
    db = v.db()
    try:
        loc = db.execute("SELECT ym, page FROM comment_loc WHERE id=?", (cid,)).fetchone()
        if loc:
            return v.links.month(loc["ym"], loc["page"]) + f"#c{cid}"
        row = db.execute("SELECT entry_id, own_post FROM comments WHERE id=?", (cid,)).fetchone()
        if row is None:
            return None
        eid = row["entry_id"]
        if row["own_post"]:
            return v.links.post(eid) + f"#c{cid}"
        idx = v.entry_index(db, eid, {})
        if idx and cid in idx[0]:
            anc = ancestors(cid, idx[0])
            root = next((a for a in anc if a in idx[0]), cid)
            g = db.execute("SELECT ym, page FROM month_groups WHERE entry_id=? AND root_id=? ORDER BY ym DESC LIMIT 1",
                           (eid, root)).fetchone()
            if g:
                return v.links.month(g["ym"], g["page"]) + f"#c{cid}"
        return comment_url(eid, cid)
    finally:
        db.close()


# ---------------------------------------------------------------------- search
SEARCH_HELP = (
    f'<details class="shelp"><summary>{icon("help")}Как искать</summary><div class="shelp-list">'
    '<code>катана</code><span>все формы слова (катану, катаной) и слова, которые с него начинаются</span>'
    '<code>"точная фраза"</code><span>слова именно в таком виде и порядке</span>'
    '<code>-слово</code><span>исключить из результатов; можно и фразу: -"фраза"</span>'
    '<code>игра OR фильм</code><span>любое из слов; вместо OR можно | или ИЛИ</span>'
    '<code>косп*</code><span>только по началу слова, без других форм</span>'
    '<span class="full">Опечатки, латинские буквы внутри русских слов и неверная раскладка (<i>rjcgktq</i> → косплей) '
    'исправляются сами, рядом появится ссылка «искать как написано». Выше — документы, где слова стоят рядом.</span>'
    '</div></details>')
KINDS = [("", "Везде"), ("p", "Посты"), ("c", "Комментарии пользователя"), ("o", "Комментарии других")]
SORTS = [("rank", "По релевантности"), ("date", "Сначала новые"), ("old", "Сначала старые")]
PER_PAGE = 50


def _year_range(year: str) -> tuple[int | None, int | None]:
    if not re.fullmatch(r"\d{4}", year or ""):
        return None, None
    return (int(_dt.datetime(int(year), 1, 1, tzinfo=MSK).timestamp()),
            int(_dt.datetime(int(year) + 1, 1, 1, tzinfo=MSK).timestamp()))


def page_search(v: ArchiveView, q: str, kind: str, year: str, sort: str, page: int, exact: bool = False,
                group: bool = True, rebuild: Any = lambda action: "") -> tuple[str, str]:
    """`rebuild(action)`: a form with the "rebuild" button (the page doesn't have the CSRF token itself)."""

    def link(**ch: Any) -> str:
        params = {"q": q, "t": kind, "y": year, "s": sort, "g": "1" if group else "0", "exact": "1" if exact else ""}
        params.update({k: ("" if val is None else str(val)) for k, val in ch.items()})
        return v.links.search() + "?" + up.urlencode({k: val for k, val in params.items() if val not in ("", None)})

    def select(name: str, items: list[tuple[str, str]], cur: str, default: str, title: str) -> str:
        opts = "".join(f'<option value="{E(k)}"{" selected" if k == cur else ""}>{E(t)}</option>' for k, t in items)
        return (f'<label class="chip sel{" on" if cur != default else ""}" title="{E(title)}">'
                f'<select name="{name}" aria-label="{E(title)}">{opts}</select>{icon("keyboard_arrow_down")}</label>')

    db = v.db()
    years = sorted({r[0][:4] for r in db.execute("SELECT ym FROM months")} |
                   {ts_date(r[0])[-4:] for r in db.execute("SELECT date FROM posts")}, reverse=True)
    sort = sort if sort in ("rank", "date", "old") else "rank"
    if not v.comments_on:   # posts only: nothing to choose between, nothing to group
        kind, group = "", False
    grouping = (f'<label class="chip" title="Показывать найденные комментарии вместе с постом или веткой, где они '
                f'написаны"><input type="checkbox" name="g" value="1"{" checked" if group else ""}>'
                f'{icon("check", cls="ck")}Группировать по постам</label><input type="hidden" name="g" value="0">')
    form = (f'<form class="sform" action="{E(v.links.search())}" data-autosubmit>'
            f'<div class="sbar">{icon("search")}<input type="search" name="q" value="{E(q)}" autofocus '
            f'placeholder="{"Поиск по постам и комментариям" if v.comments_on else "Поиск по постам"}" aria-label="Что искать">'
            f'{icon_btn("arrow_forward", "Найти", submit=True)}</div>'
            f'<div class="chips scroll">{select("t", KINDS, kind, "", "Где искать") if v.comments_on else ""}'
            f'{select("y", [("", "Все годы")] + [(y, y) for y in years], year, "", "Год")}'
            f'{select("s", SORTS, sort, "rank", "Порядок")}'
            f'{grouping if v.comments_on else ""}</div></form>{SEARCH_HELP}')
    head = page_head("Поиск")
    if not q.strip():
        db.close()
        return "Поиск", head + form
    if not has_index(db):
        db.close()
        return "Поиск", head + form + banner("warn", "Поиску нужно пересобрать страницы архива — это займёт пару "
                                                     "минут и не требует интернета." + rebuild(v.links.action("render")))
    a, b = _year_range(year)
    res = search(db, q, kind, a, b, sort, page, PER_PAGE, exact)
    if res.error:
        db.close()
        return "Поиск", head + form + banner("err", E(res.error))
    notes = ""
    if res.notes:
        notes += banner("info", f'Исправлено: {E("; ".join(res.notes))}. '
                                f'<a href="{E(link(exact="1", page=None))}">Искать как написано</a>')
    elif exact:
        notes += banner("info", f'Поиск как написано, без исправлений. <a href="{E(link(exact=None))}">Включить исправления</a>')
    if res.suggestion:
        notes += banner("info", f'Возможно, вы искали: <a href="{E(link(q=res.suggestion, exact=None, page=None))}">'
                                f'{E(res.suggestion)}</a>', ic="help")

    # data for the page's hits: comments (+ their parents) and posts titles
    ids = [h.ref for h in res.hits if h.kind != "p"]
    cmts: dict[int, dict] = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for r in db.execute(f"SELECT id, entry_id, data FROM comments WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            cmts[r[0]] = unpack(r[2])
    parents = [c.get("replyTo") for c in cmts.values() if c.get("replyTo")]
    pmap: dict[int, dict] = {}
    for chunk in (parents[i:i + 500] for i in range(0, len(parents), 500)):
        for r in db.execute(f"SELECT id, data FROM comments WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            pmap[r[0]] = unpack(r[1])
    eids = {h.entry for h in res.hits if h.entry}
    entries: dict[int, tuple[str, str | None, bool]] = {}
    for chunk in (list(eids)[i:i + 500] for i in range(0, len(eids), 500)):
        for r in db.execute(f"SELECT id, title, subsite_name, own FROM entries WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            entries[r[0]] = (r[1] or "", r[2], bool(r[3]))
    db.close()
    mk = res.marker

    def who(aid: Any) -> str:
        return f'<span class="ca">{v.cv.avatar_html(aid)}{E(v.cv.author_name(aid))}</span>'

    def comment_hit(h: Any) -> str:
        c = cmts.get(h.ref) or {}
        snip, _ = mk.snippet(h.body, 300) if mk else (E(h.body[:300]), 0)
        ctx = ""
        par = pmap.get(c.get("replyTo") or 0)
        if par:
            ptxt = comment_text(par.get("text")).text
            psnip, _ = mk.snippet(ptxt, 160) if mk else (E(ptxt[:160]), 0)
            ctx = (f'<div class="sh-ctx" title="В ответ на комментарий">{icon("subdirectory_arrow_right")}'
                   f'{who(par.get("author"))}<span>{psnip}</span></div>')
        media = mi("image", str(len(c["media"])), "Вложения") if c.get("media") else ""
        return (f'<div class="sh{" mine" if h.kind == "c" else ""}">{ctx}<a class="sh-main" href="{E(v.links.go(h.ref))}">'
                f'<div class="c-h">{who(h.author)}<span class="cd">{ts_human(h.date)}</span>{media}</div>'
                f'<div class="sh-text">{snip}</div></a></div>')

    def post_hit(h: Any) -> str:
        snip, _ = mk.snippet(h.body, 360) if mk else (E(h.body[:360]), 0)
        title, _ = mk.snippet(h.title, 300) if mk else (E(h.title), 0)
        return (f'<a class="sh-main sh-post" href="{E(v.links.post(h.ref))}"><div class="sh-pt">{title}</div>'
                f'<div class="meta">{mi("article", "Пост")}<span>{ts_human(h.date)}</span></div>'
                f'<div class="sh-text">{snip}</div></a>')

    def header(eid: int | None, n: int = 0) -> str:
        title, sub, own = entries.get(eid or 0, ("", None, False))
        n_hits = badge(count_label(n, "совпадение", "совпадения", "совпадений"), cls="sg-n") if n > 1 else ""
        return _entry_head(v, eid, title or "Пост", sub, own, n_hits, cls="sg-h")

    blocks = []
    if group:
        groups: dict[Any, list] = {}
        for h in res.hits:
            groups.setdefault(h.ref if h.kind == "p" else (h.entry or f"x{h.ref}"), []).append(h)
        for hs in groups.values():
            post = [h for h in hs if h.kind == "p"]
            rest = [h for h in hs if h.kind != "p"]
            head_h = post_hit(post[0]) if post else header(hs[0].entry, len(rest))
            blocks.append(f'<section class="sg card">{head_h}{"".join(comment_hit(h) for h in rest)}</section>')
    else:
        for h in res.hits:
            blocks.append(f'<section class="sg card">{post_hit(h) if h.kind == "p" else header(h.entry) + comment_hit(h)}</section>')

    npages = (res.total + PER_PAGE - 1) // PER_PAGE
    empty = empty_state("search_off", "Ничего не найдено",
                        "Попробуйте другие слова, уберите фильтры или ищите по началу слова: <b>косп*</b>.")
    body = (head + form + notes + f'<p class="sres-n">Найдено: {num(res.total)}</p>'
            f'<div class="sres">{"".join(blocks) or empty}</div>' + pagination(page, npages, lambda n: link(page=n)))
    return f"Поиск: {q}", body
