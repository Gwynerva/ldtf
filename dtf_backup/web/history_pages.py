"""History pages of an archive (history.py keeps the data, viewdb copies it into view.sqlite):

  /u/<nick>/p/<id>/history          a post's timeline: the current version, earlier versions, removals and returns
  /u/<nick>/p/<id>/history/<hid>    one earlier version: what the next version changed (default) or the whole of it
  /u/<nick>/c/<id>/history          a comment: every version with the words that changed, removals and returns
  /u/<nick>/changes                 every change of the archive, newest first, with filters
"""

from __future__ import annotations

import html
import json
import sqlite3
import urllib.parse as up
from typing import TYPE_CHECKING, Any

from ..blocks import render_blocks
from ..guard import state_title
from ..history import block_ops, summary, word_diff
from ..normalize import comment_text
from ..state import unpack
from ..util import count_label, short, ts_date, ts_human
from ..viewdb import post_plain_text, slim_comment
from .icons import icon
from .ui import btn, empty_state, fold, page_head, pagination

if TYPE_CHECKING:
    from .viewer import ArchiveView

E = html.escape
TEXT_TYPES = {"text", "header", "quote", "incut", "list", "code"}
EVENTS = {"edit": ("edit_note", "Правки"), "removed": ("delete", "Удаления"), "restored": ("settings_backup_restore", "Возвращения")}
PER_PAGE = 50


def since_note(v: "ArchiveView") -> str:
    since = v.meta.get("history_since")
    if not since:
        return ""
    return (f'<p class="muted small tl-since">{icon("info")}<span>LDTF следит за правками с {ts_date(since)}: '
            f'более ранние версии он не видел.</span></p>')


def _rows(db: sqlite3.Connection, kind: str, item: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM history WHERE kind=? AND item_id=? ORDER BY at, id", (kind, item)).fetchall()


def _event_li(r: sqlite3.Row, what: str) -> str:
    if r["event"] == "removed":
        return (f'<li class="tl-i tl-rm"><span class="tl-dot">{icon("delete")}</span><div class="tl-b">'
                f'<b>{E(what)} {E(state_title(r["state"]) if r["state"] else "пропал с DTF")}</b>'
                f'<span class="tl-s">LDTF заметил {ts_human(r["at"])}; в архиве осталась прежняя версия</span></div></li>')
    return (f'<li class="tl-i tl-back"><span class="tl-dot">{icon("settings_backup_restore")}</span><div class="tl-b">'
            f'<b>Снова есть на DTF</b><span class="tl-s">{ts_human(r["at"])}</span></div></li>')


# ---------------------------------------------------------------------- posts
def _post_versions(db: sqlite3.Connection, v: "ArchiveView", pid: int) -> tuple[dict | None, list[sqlite3.Row]]:
    row = db.execute("SELECT raw FROM posts WHERE id=?", (pid,)).fetchone()
    return (unpack(row[0]) if row else None), _rows(db, "post", pid)


def page_post_history(v: "ArchiveView", pid: int) -> tuple[str, str] | None:
    db = v.db()
    cur, rows = _post_versions(db, v, pid)
    db.close()
    if cur is None:
        return None
    edits = [r for r in rows if r["event"] == "edit"]
    nxt: dict[int, dict] = {}   # what replaced each version: the next edit's body, or the current post
    for i, r in enumerate(edits):
        nxt[r["id"]] = unpack(edits[i + 1]["body"]) if i + 1 < len(edits) else cur
    L = v.links
    title = cur.get("title") or "Без заголовка"
    site = (cur.get("_site") or {}).get("state")
    items = [f'<li class="tl-i tl-cur"><span class="tl-dot">{icon("article")}</span><div class="tl-b">'
             f'<b>Текущая версия</b><span class="tl-s">от {ts_human(cur.get("dateModified") or cur.get("date"))}'
             + (f' · {E(state_title(site))}, в архиве сохранена эта версия' if site else "") +
             f'</span><div class="tl-a">{btn("Открыть пост", "tonal sm", "article", href=L.post(pid))}</div></div></li>']
    for r in reversed(rows):
        if r["event"] != "edit":
            items.append(_event_li(r, "Пост"))
            continue
        old, new = unpack(r["body"]), nxt[r["id"]]
        what = summary(old.get("blocks") or [], new.get("blocks") or [], (old.get("title") or "") != (new.get("title") or ""))
        items.append(f'<li class="tl-i"><span class="tl-dot">{icon("edit_note")}</span><div class="tl-b">'
                     f'<b>Версия от {ts_human(r["version_date"])}</b>'
                     f'<span class="tl-s">Изменена на DTF, LDTF заметил {ts_human(r["at"])}: {E(what)}</span>'
                     f'<div class="tl-a">{btn("Что изменилось", "tonal sm", "difference", href=L.post_version(pid, r["id"]))}'
                     f'{btn("Целиком", "text sm", href=L.post_version(pid, r["id"], full=True))}</div></div></li>')
    n = len(edits) + 1
    sub = (f'<a href="{E(L.post(pid))}">{E(title)}</a> · '
           + (count_label(n, "версия", "версии", "версий") if edits else "правок не было"))
    body = (page_head("История поста", sub) +
            f'<ol class="tl card" data-versions="{E(json.dumps([r["id"] for r in reversed(edits)]))}">{"".join(items)}</ol>'
            + since_note(v))
    return f"История: {title}", body


def page_post_version(v: "ArchiveView", pid: int, hid: int, full: bool) -> tuple[str, str] | None:
    db = v.db()
    cur, rows = _post_versions(db, v, pid)
    db.close()
    edits = [r for r in rows if r["event"] == "edit"]
    i = next((k for k, r in enumerate(edits) if r["id"] == hid), None)
    if cur is None or i is None:
        return None
    old = unpack(edits[i]["body"])
    new = unpack(edits[i + 1]["body"]) if i + 1 < len(edits) else cur
    new_date = edits[i + 1]["version_date"] if i + 1 < len(edits) else (cur.get("dateModified") or cur.get("date"))
    L = v.links
    ctx = v.ctx(f"post:{pid}", f"post {pid} history")
    seg = (f'<nav class="seg seg-links" aria-label="Вид">'
           f'<a href="{E(L.post_version(pid, hid))}"{" class=on aria-current=page" if not full else ""}>'
           f'{icon("difference")}Изменения</a>'
           f'<a href="{E(L.post_version(pid, hid, full=True))}"{" class=on aria-current=page" if full else ""}>'
           f'{icon("article")}Версия целиком</a></nav>')
    head = page_head("Прежняя версия поста", f'от {ts_human(edits[i]["version_date"])} · '
                     f'<a href="{E(L.post(pid))}">{E(short(cur.get("title") or "Без заголовка", 90))}</a>',
                     btn("История", "text", "history", href=L.post_history(pid)))
    if full:
        bh, _, _ = render_blocks(old.get("blocks") or [], ctx)
        note = (f'<div class="banner info">{icon("history", fill=True)}<div class="banner-t">Это прежняя версия от '
                f'{ts_human(edits[i]["version_date"])}. <a href="{E(L.post(pid))}">Текущая версия</a></div></div>')
        art = f'<article class="post card ver">{note}<h1>{E(old.get("title") or "Без заголовка")}</h1>{bh}</article>'
        return f"Версия: {old.get('title') or pid}", head + seg + art
    parts = []
    ot, nt = old.get("title") or "", new.get("title") or ""
    if ot != nt:
        parts.append(f'<h1 class="d-title">{word_diff(ot, nt)}</h1>')
    else:
        parts.append(f'<h1>{E(nt or "Без заголовка")}</h1>')
    ob, nb = old.get("blocks") or [], new.get("blocks") or []

    def show(b: Any, cls: str, label: str = "") -> str:
        h, _, _ = render_blocks([b], ctx)
        lab = f'<span class="d-lab">{E(label)}</span>' if label else ""
        return f'<div class="dblk {cls}">{lab}{h}</div>'

    changed = 0
    for op, ia, ib in block_ops(ob, nb):
        if op == "equal":
            same = "".join(show(nb[j], "same") for j in ib)
            if len(ib) > 2:
                parts.append(fold(f'{count_label(len(ib), "блок", "блока", "блоков")} без изменений', same, "fold d-same"))
            else:
                parts.append(same)
            continue
        changed += 1
        pairs = list(zip(ia, ib)) if op == "replace" else []
        for a, b in pairs:
            x, y = ob[a], nb[b]
            if (isinstance(x, dict) and isinstance(y, dict) and x.get("type") == y.get("type") in TEXT_TYPES
                    and not x.get("hidden") and not y.get("hidden")):
                diff = word_diff(post_plain_text({"blocks": [x]}), post_plain_text({"blocks": [y]}))
                parts.append(f'<div class="dblk chg"><span class="d-lab">изменено</span>'
                             f'<div class="blk d-text d-{E(str(y.get("type")))}">{diff}</div></div>')
            else:
                parts.append(show(x, "del", "было") + show(y, "ins", "стало"))
        for a in ia[len(pairs):]:
            parts.append(show(ob[a], "del", "удалено"))
        for b in ib[len(pairs):]:
            parts.append(show(nb[b], "ins", "добавлено"))
    if not changed and ot == nt:
        parts.append('<p class="muted">Текст и медиа не изменились — DTF поменял только оформление.</p>')
    intro = (f'<p class="d-intro muted small">{icon("difference")}<span>Что изменилось в версии от '
             f'{ts_human(new_date)} по сравнению с версией от {ts_human(edits[i]["version_date"])}: '
             f'<ins>добавленное</ins> и <del>удалённое</del>.</span></p>')
    return f"Изменения: {nt or pid}", head + seg + intro + f'<article class="post card diff">{"".join(parts)}</article>'


# ---------------------------------------------------------------------- comments
def page_comment_history(v: "ArchiveView", cid: int) -> tuple[str, str] | None:
    db = v.db()
    row = db.execute("SELECT entry_id, data FROM comments WHERE id=?", (cid,)).fetchone()
    rows = _rows(db, "comment", cid)
    title = None
    if row and row["entry_id"]:
        t = db.execute("SELECT title FROM entries WHERE id=?", (row["entry_id"],)).fetchone()
        title = t[0] if t else None
    db.close()
    if row is None and not rows:
        return None
    cur = unpack(row["data"]) if row else None
    eid = row["entry_id"] if row else (rows[0]["entry_id"] if rows else None)
    cv = v.cv
    edits = [r for r in rows if r["event"] == "edit"]
    texts = [comment_text(unpack(r["body"]).get("text")).text for r in edits] + [
        comment_text((cur or {}).get("text")).text]
    items = []
    if cur is not None:
        site = cur.get("site")
        items.append(f'<li class="tl-i tl-cur"><span class="tl-dot">{icon("chat_bubble")}</span><div class="tl-b">'
                     f'<b>{"Версия в архиве" if site else "Текущая версия"}</b>'
                     f'<span class="tl-s">{E(state_title(site)) + ", сохранён этот текст" if site else ts_human(cur.get("date"))}'
                     f'</span><div class="tl-c">{cv.one(dict(cur, hist=0), eid, False)}</div></div></li>')
    idx = len(edits)
    for r in reversed(rows):
        if r["event"] != "edit":
            items.append(_event_li(r, "Комментарий"))
            continue
        idx -= 1
        old = unpack(r["body"])
        c = slim_comment(old, {})
        c["text"] = ""   # the words come with the diff below
        c["media"] = old.get("media") or []
        diff = word_diff(texts[idx], texts[idx + 1])
        items.append(f'<li class="tl-i"><span class="tl-dot">{icon("edit_note")}</span><div class="tl-b">'
                     f'<b>Версия от {ts_human(r["version_date"])}</b>'
                     f'<span class="tl-s">Изменён на DTF, LDTF заметил {ts_human(r["at"])}. Подсвечено, что поменялось '
                     f'в следующей версии</span><div class="tl-c"><div class="c ver">'
                     f'<div class="c-t d-text">{diff or "<span class=muted>(без текста)</span>"}</div>'
                     + (f'<div class="c-m pswp-gallery">{cv.media_parts(c)[0]}</div>' if c["media"] else "") +
                     '</div></div></div></li>')
    L = v.links
    where = (f'<a href="{E(L.go(cid))}">к комментарию</a>' + (f' · {E(short(title, 90))}' if title else "")
             if cur is not None else "")
    body = page_head("История комментария", where) + f'<ol class="tl card">{"".join(items)}</ol>' + since_note(v)
    return "История комментария", body


# ---------------------------------------------------------------------- all changes
def page_changes(v: "ArchiveView", event: str, kind: str, page: int) -> tuple[str, str]:
    L = v.links
    where, args = ["1"], []
    if event in EVENTS:
        where.append("h.event=?")
        args.append(event)
    if kind in ("post", "comment"):
        where.append("h.kind=?")
        args.append(kind)
    if not v.comments_on:
        where.append("h.kind='post'")
    db = v.db()
    total = db.execute(f"SELECT COUNT(*) FROM history h WHERE {' AND '.join(where)}", args).fetchone()[0]
    npages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, npages))
    rows = db.execute(f"SELECT h.id, h.kind, h.item_id, h.entry_id, h.at, h.event, h.state FROM history h "
                      f"WHERE {' AND '.join(where)} ORDER BY h.at DESC, h.id DESC LIMIT ? OFFSET ?",
                      args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    pids = {r["item_id"] for r in rows if r["kind"] == "post"} | {r["entry_id"] for r in rows if r["entry_id"]}
    titles: dict[int, str] = {}
    for chunk in (list(pids)[i:i + 500] for i in range(0, len(pids), 500)):
        q = ",".join("?" * len(chunk))
        titles.update({r[0]: r[1] or "" for r in db.execute(f"SELECT id, title FROM entries WHERE id IN ({q})", chunk)})
        titles.update({r[0]: r[1] or "" for r in db.execute(f"SELECT id, title FROM posts WHERE id IN ({q})", chunk)})
    cids = [r["item_id"] for r in rows if r["kind"] == "comment"]
    cmts: dict[int, dict] = {}
    for chunk in (cids[i:i + 500] for i in range(0, len(cids), 500)):
        q = ",".join("?" * len(chunk))
        cmts.update({r[0]: unpack(r[1]) for r in db.execute(f"SELECT id, data FROM comments WHERE id IN ({q})", chunk)})
    db.close()

    def link(**ch: Any) -> str:
        params = {"e": event, "k": kind, "page": None}
        params.update(ch)
        q = up.urlencode({k: val for k, val in params.items() if val})
        return L.changes() + (f"?{q}" if q else "")

    def chip(label: str, on: bool, href: str) -> str:
        return f'<a class="chip{" on" if on else ""}" href="{E(href)}">{icon("check", cls="ck") if on else ""}{E(label)}</a>'
    chips = ('<div class="chips scroll filters">' + chip("Все", not event, link(e=None))
             + "".join(chip(t, event == k, link(e=k)) for k, (_, t) in EVENTS.items())
             + ('<span class="chips-div"></span>' + chip("Посты", kind == "post", link(k=None if kind == "post" else "post"))
                + chip("Комментарии", kind == "comment", link(k=None if kind == "comment" else "comment"))
                if v.comments_on else "") + "</div>")
    lis = []
    for r in rows:
        ic, _ = EVENTS.get(r["event"], ("history", ""))
        ev = {"edit": "изменён", "removed": state_title(r["state"]) if r["state"] else "пропал с DTF",
              "restored": "снова есть на DTF"}.get(r["event"], r["event"])
        if r["kind"] == "post":
            href = L.post_history(r["item_id"])
            t = titles.get(r["item_id"]) or "Пост"
            what = f'<span class="li-t">{E(t)}</span><span class="li-s">Пост {E(ev)} · {ts_human(r["at"])}</span>'
        else:
            c = cmts.get(r["item_id"]) or {}
            href = L.comment_history(r["item_id"])
            text = comment_text(c.get("text")).text if c else ""
            who = v.cv.author_name(c.get("author")) if c else "Комментарий"
            post = titles.get(r["entry_id"] or 0)
            what = (f'<span class="li-t">{E(who)}: {E(short(text, 140)) or "без текста"}</span>'
                    f'<span class="li-s">Комментарий {E(ev)} · {ts_human(r["at"])}'
                    + (f' · {E(short(post, 70))}' if post else "") + "</span>")
        lis.append(f'<div class="li ch-li ch-{r["event"]}"><span class="ch-ic">{icon(ic)}</span><div class="li-body">'
                   f'<a class="li-link" href="{E(href)}">{what}</a></div></div>')
    head = page_head("Изменения на DTF", "Правки, удаления и возвращения, которые LDTF заметил при синхронизациях",
                     n=total if total else None)
    if not lis:
        msg = ("Пока ни одной правки и удаления: всё в архиве совпадает с тем, что есть на DTF." if not (event or kind)
               else "С такими фильтрами ничего нет.")
        return "Изменения на DTF", head + chips + empty_state("history", "Изменений нет", msg) + since_note(v)
    body = (head + chips + pagination(page, npages, lambda n: link(page=n if n > 1 else None), compact=True)
            + f'<div class="card list ch-list">{"".join(lis)}</div>'
            + pagination(page, npages, lambda n: link(page=n if n > 1 else None)) + since_note(v))
    return "Изменения на DTF", body


def changes_count(v: "ArchiveView") -> int:
    db = v.db()
    try:
        q = "SELECT COUNT(*) FROM history" + ("" if v.comments_on else " WHERE kind='post'")
        return db.execute(q).fetchone()[0]
    finally:
        db.close()
