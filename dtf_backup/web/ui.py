"""Shared UI pieces: URL scheme, page shell (app bar), Material 3 markup components, comment rendering."""

from __future__ import annotations

import hashlib
import html
import json
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from ..api import comment_url
from ..blocks import (STUB_ICONS, Ctx, Report, _link_card, _video_parts, b_osnova_embed, json_spoiler, media_html, media_md,
                      media_norm, media_stub)
from ..guard import state_title, title
from ..media import avatar_key
from ..normalize import EXT_LINK, Linker, MediaResolver, comment_text, media_info
from ..reactions import Reactions
from ..util import COMMENTS, POSTS, count_label, num, plural, rub, ts_human
from .icons import icon

E = html.escape
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _assets_version() -> str:
    h = hashlib.sha1()
    for f in sorted(p for p in ASSETS.rglob("*") if p.is_file()):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


AV = _assets_version()
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def month_title(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1].capitalize()} {y}"


class Links:
    """URL scheme of the app for one archive."""

    def __init__(self, nick: str):
        self.nick = nick
        self.base = "/u/" + urllib.parse.quote(nick)

    def home(self) -> str: return self.base + "/"
    def posts(self) -> str: return self.base + "/posts"
    def post(self, pid: int) -> str: return f"{self.base}/p/{int(pid)}"
    def comments(self) -> str: return self.base + "/comments"
    def month(self, ym: str, page: int = 1) -> str: return f"{self.base}/c/{ym}" + (f"?page={page}" if page > 1 else "")
    def go(self, cid: int) -> str: return f"{self.base}/go/c/{int(cid)}"
    def search(self) -> str: return self.base + "/search"
    def donations(self) -> str: return self.base + "/donations"
    def changes(self) -> str: return self.base + "/changes"
    def post_history(self, pid: int) -> str: return f"{self.base}/p/{int(pid)}/history"
    def comment_history(self, cid: int) -> str: return f"{self.base}/c/{int(cid)}/history"

    def post_version(self, pid: int, hid: int, full: bool = False) -> str:
        return f"{self.base}/p/{int(pid)}/history/{int(hid)}" + ("?full=1" if full else "")
    def sync(self) -> str: return self.base + "/sync"
    def settings(self) -> str: return self.base + "/settings"
    def action(self, name: str) -> str: return f"{self.base}/{name}"   # POST: sync/start, sync/stop, render, guard, delete

    @staticmethod
    def media(path: str) -> str: return "/" + path.lstrip("/")

    @staticmethod
    def asset(name: str) -> str: return f"/assets/{name}?v={AV}"


def avatar(src: str | None, cls: str = "", lazy: bool = True) -> str:
    """Round avatar (the surrounding CSS sets the size, --s); the person icon without a picture - also when a picture
    from DTF (not in the archive) fails to load (app.js)."""
    c = f" {cls}" if cls else ""
    if src:
        rem = ' data-remote="avatar"' if src.startswith("http") else ""
        return f'<img class="av{c}" src="{E(src)}" alt=""{" loading=lazy" if lazy else ""}{rem}>'
    return f'<span class="av0{c}">{icon("person")}</span>'


def avatar_src(resolver: MediaResolver, avatar: Any) -> str | None:
    """The archived small avatar, else DTF's; None when there is none or DTF reported it deleted."""
    key = avatar_key(avatar)
    if not key or resolver.gone(key):
        return None
    loc = resolver.local(key)
    return Links.media(loc["path"]) if loc else key


def archive_link(a: dict) -> tuple[str, str]:
    """(href, line under the name) of an archive entry: a built archive opens, one still building shows its sync."""
    L = Links(a["nick"])
    if a.get("built"):
        return L.home(), counts_line(a.get("posts") or 0, a.get("comments") or 0, a.get("comments_on", True))
    return L.sync(), "архив ещё собирается"


def counts_line(posts: int, comments: int, with_comments: bool = True) -> str:
    """"12 постов · 340 комментариев"; an archive of posts only: "12 постов · только посты"."""
    return f"{count_label(posts, *POSTS)} · " + (count_label(comments, *COMMENTS) if with_comments else "только посты")


# ---------------------------------------------------------------------- components (Material 3 markup)
def btn(label: str, kind: str = "", ic: str | None = None, href: str | None = None, submit: bool = True,
        title: str = "", attrs: str = "") -> str:
    """kind: "" (filled) | tonal | outlined | text | danger | "danger tonal" ... (+ sm)."""
    cls = "btn" + (f" {kind}" if kind else "")
    t = f' title="{E(title)}"' if title else ""
    inner = (icon(ic) if ic else "") + E(label)
    if href is not None:
        return f'<a class="{cls}" href="{E(href)}"{t}{attrs}>{inner}</a>'
    return f'<button class="{cls}" type="{"submit" if submit else "button"}"{t}{attrs}>{inner}</button>'


def icon_btn(ic: str, title: str, href: str | None = None, cls: str = "", attrs: str = "", submit: bool = False) -> str:
    c = "icon-btn" + (f" {cls}" if cls else "")
    lab = f' title="{E(title)}" aria-label="{E(title)}"'
    if href is not None:
        return f'<a class="{c}" href="{E(href)}"{lab}{attrs}>{icon(ic)}</a>'
    return f'<button class="{c}" type="{"submit" if submit else "button"}"{lab}{attrs}>{icon(ic)}</button>'


def menu(ic: str, title: str, items: str, right: bool = True, cls: str = "") -> str:
    """Overflow menu on <details>; items are menu_item() links or forms with a .menu-item button."""
    c = "menu" + (" right" if right else "") + (f" {cls}" if cls else "")
    return (f'<details class="{c}"><summary class="icon-btn" title="{E(title)}" aria-label="{E(title)}">{icon(ic)}</summary>'
            f'<div class="menu-pop" role="menu">{items}</div></details>')


def menu_item(label: str, ic: str, href: str | None = None, on: bool = False, title: str = "") -> str:
    c = "menu-item" + (" on" if on else "")
    t = f' title="{E(title)}"' if title else ""
    if href is None:
        return f'<button class="{c}" type="submit" role="menuitem"{t}>{icon(ic)}{E(label)}</button>'
    return f'<a class="{c}" href="{E(href)}" role="menuitem"{t}>{icon(ic)}{E(label)}</a>'


def page_head(title: str, sub: str = "", actions: str = "", n: int | None = None, raw_title: bool = False) -> str:
    """Page title row. `title` is escaped unless raw_title; `sub` and `actions` are HTML."""
    t = title if raw_title else E(title)
    cnt = f'<span class="n">{num(n)}</span>' if n is not None else ""
    return (f'<div class="ph"><div class="ph-t"><h1>{t}{cnt}</h1>{f"<p class=ph-sub>{sub}</p>" if sub else ""}</div>'
            f'{f"<div class=ph-act>{actions}</div>" if actions else ""}</div>')


def sec_head(title: str, n: int | None = None, action: str = "") -> str:
    cnt = f'<span class="n">{num(n)}</span>' if n is not None else ""
    return f'<div class="sec-h"><h2>{E(title)}{cnt}</h2>{action}</div>'


def tabs(items: list[tuple[str, str, str, str]], active: str) -> str:
    """items: (href, label, icon, key)."""
    return '<nav class="tabs">' + "".join(
        f'<a class="tab{" on" if k == active else ""}" href="{E(h)}"{" aria-current=page" if k == active else ""}>'
        f'{icon(i)}{E(t)}</a>' for h, t, i, k in items) + "</nav>"


def manage_tabs(links: "Links", active: str) -> str:
    """Tabs shared by the archive management pages (sync / settings)."""
    return tabs([(links.sync(), "Синхронизация", "sync", "sync"), (links.settings(), "Настройки", "tune", "settings")],
                active)


APP_TABS = [("/app", "Основные", "settings", "app"), ("/app/reactions", "Реакции", "add_reaction", "reactions")]
APP_PAGES = {k for _, _, _, k in APP_TABS} | {"diagnostics"}   # the account menu marks "Настройки приложения" on them


def app_tabs(active: str) -> str:
    """Tabs of the app settings (they apply to every archive)."""
    return tabs(APP_TABS, active)


def pagination(cur: int, total: int, link: Callable[[int], str], compact: bool = False) -> str:
    """Numbered pages with a window around the current one: < 1 … 4 5 [6] 7 8 … 14 >."""
    if total <= 1:
        return ""
    near = 1 if compact else 2
    show = sorted({1, total, *range(max(1, cur - near), min(total, cur + near) + 1)})
    out, prev = [], 0
    for n in show:
        if n - prev > 1:
            out.append('<span class="gap">…</span>')
        out.append(f'<span class="on" aria-current="page">{n}</span>' if n == cur else f'<a href="{E(link(n))}">{n}</a>')
        prev = n

    def arrow(n: int, ic: str, t: str) -> str:
        if 1 <= n <= total:
            return f'<a href="{E(link(n))}" title="{t}" aria-label="{t}">{icon(ic)}</a>'
        return f'<span class="dis" aria-hidden="true">{icon(ic)}</span>'
    return (f'<nav class="pgn{" compact" if compact else ""}" aria-label="Страницы">'
            f'{arrow(cur - 1, "chevron_left", "Предыдущая страница")}{"".join(out)}'
            f'{arrow(cur + 1, "chevron_right", "Следующая страница")}</nav>')


def pager(prev: tuple[str, str] | None, nxt: tuple[str, str] | None, prev_label: str, next_label: str) -> str:
    """Two outlined cards: (href, title) of the previous / next item."""
    if not prev and not nxt:
        return ""
    a = (f'<a class="prev" href="{E(prev[0])}"><small>{icon("chevron_left")}{E(prev_label)}</small><b>{E(prev[1])}</b></a>'
         if prev else "<span></span>")
    b = (f'<a class="next" href="{E(nxt[0])}"><small>{E(next_label)}{icon("chevron_right")}</small><b>{E(nxt[1])}</b></a>'
         if nxt else "")
    return f'<nav class="pager">{a}{b}</nav>'


BANNER_ICONS = {"info": "info", "ok": "check_circle", "err": "error", "warn": "warning"}


def banner(kind: str, html_: str, ic: str | None = None) -> str:
    return (f'<div class="banner {kind}" role="{"alert" if kind == "err" else "status"}">'
            f'{icon(ic or BANNER_ICONS.get(kind, "info"), fill=True)}<div class="banner-t">{html_}</div></div>')


def guard_banner(acc: dict) -> str:
    """Every page of an archive whose sync the guard stopped says so and links to the decision."""
    g = acc.get("guard") or {}
    what = title(g.get("kind"))
    return (f'<div class="banner err guard-banner" role="alert">{icon("gpp_maybe", fill=True)}'
            f'<div class="banner-t"><b>Синхронизация остановлена:</b> {E(what)}. Архив не изменён.</div>'
            f'<a class="btn text" href="{E(Links(acc["nick"]).sync())}">Разобраться</a></div>')


def fold(summary: str, body: str, cls: str = "fold", attrs: str = "") -> str:
    """A disclosure with a chevron (style.css draws them all alike); `summary` and `body` are HTML."""
    return f'<details class="{cls}"{attrs}><summary>{icon("keyboard_arrow_down")}{summary}</summary>{body}</details>'


def badge(text: str, ic: str | None = None, cls: str = "", title: str = "") -> str:
    """A small label next to a title; `text` is escaped."""
    c = f" {cls}" if cls else ""
    t = f' title="{E(title)}"' if title else ""
    return f'<span class="badge{c}"{t}>{icon(ic) if ic else ""}{E(text)}</span>'


def hidden_input(name: str, value: Any) -> str:
    return f'<input type="hidden" name="{E(name)}" value="{E(str(value))}">'


def snackbar(text: str) -> str:
    return f'<div class="snackbar" role="status">{E(text)}</div>'


def empty_state(ic: str, title: str, text: str = "", action: str = "", tag: str = "h2") -> str:
    return f'<div class="empty">{icon(ic)}<{tag}>{E(title)}</{tag}>{f"<p>{text}</p>" if text else ""}{action}</div>'


def stat(value: str, label: str, href: str | None = None) -> str:
    inner = f"<b>{value}</b><span>{E(label)}</span>"
    return f'<a class="stat" href="{E(href)}">{inner}</a>' if href else f'<div class="stat">{inner}</div>'


def mi(ic: str, text: str, title: str = "") -> str:
    """Small meta item: icon + text (text is HTML)."""
    t = f' title="{E(title)}"' if title else ""
    return f'<span class="mi"{t}>{icon(ic)}{text}</span>'


RING = ('<svg class="ring" viewBox="0 0 24 24" aria-hidden="true"><circle class="rt" cx="12" cy="12" r="9"/>'
        '<circle class="rb" cx="12" cy="12" r="9" stroke-dasharray="56.55" stroke-dashoffset="56.55"/></svg>')
# applied before the stylesheet: saved choice or the OS preference, so the page never flashes the other theme
THEME_JS = ('(function(){var d=document.documentElement,t=null;try{t=localStorage.getItem("dtf-theme")}catch(e){}'
            'var m=window.matchMedia&&matchMedia("(prefers-color-scheme: dark)");'
            'd.setAttribute("data-theme",t||(m&&m.matches?"dark":"light"));'
            'var c=document.querySelector("meta[name=theme-color]");if(c&&d.getAttribute("data-theme")=="dark")c.content="#111318";'
            'if(!t&&m&&m.addEventListener)m.addEventListener("change",function(e){try{if(localStorage.getItem("dtf-theme"))return}'
            'catch(x){}d.setAttribute("data-theme",e.matches?"dark":"light")})})();')
# before any picture loads: note the files from DTF (not in the archive) that fail, app.js puts placeholders there
MEDIA_JS = ('document.addEventListener("error",function(e){var t=e.target;if(t&&t.hasAttribute&&t.hasAttribute("data-remote")'
            '&&!t.hasAttribute("data-failed"))t.setAttribute("data-failed","net")},true);')
# what app.js builds pages from: icons by name and the placeholder of a file that failed to load
PAGE_TEMPLATES = ('<template id="icons">' + "".join(f'<i data-n="{n}">{icon(n)}</i>' for n in ("arrow_back", "person"))
                  + "".join(f'<i data-n="stub-{k}">{icon(n)}</i>' for k, n in STUB_ICONS.items()) + "</template>"
                  f'<template id="mstub">{media_stub("image")}</template>')
NAV = [("home", "index", "Главная", "home"), ("posts", "posts", "Посты", "article"),
       ("comments", "comments", "Комментарии", "forum"), ("search", "search", "Поиск", "search")]
MANAGE = {"sync", "settings"}


class Shell:
    """Page layout: the same app bar on every page. `accounts` feeds the archive switcher:
    [{nick, name, avatar, posts, comments, built}]; `current` is the archive shown in the bar."""

    def __init__(self, csrf: str):
        self.csrf = csrf

    def appbar(self, current: dict | None, accounts: list[dict], active: str) -> str:
        links = Links(current["nick"]) if current else None
        if current:
            who, label = avatar(current.get("avatar")), E(current["name"])
        else:
            who, label = avatar(None), "Архивы"
        items = []
        for a in accounts:
            on = bool(current and a["nick"] == current["nick"])
            href, sub = archive_link(a)
            items.append(
                f'<a class="acct-item{" on" if on else ""}" data-nick="{E(a["nick"])}" href="{E(href)}">'
                f'{avatar(a.get("avatar"))}<span class="acct-t"><b>{E(a["name"])}</b><small>@{E(a["nick"])}</small>'
                f'<small>{sub}</small></span><span class="acct-sync" title="Идёт синхронизация" hidden>{icon("sync")}</span>'
                f'{icon("check", cls="acct-on") if on else ""}</a>')
        if items:
            items.append('<hr class="menu-div">')
        items.append(menu_item("Добавить пользователя", "person_add", "/add", on=active == "add"))
        items.append(menu_item("Все архивы", "inventory_2", "/archives", on=active == "archives"))
        items.append(menu_item("Блоки DTF", "widgets", "/blocks", on=active == "blocks"))
        items.append(menu_item("Настройки приложения", "settings", "/app", on=active in APP_PAGES))
        acct = (f'<details class="acct"><summary title="Сменить архив" aria-label="Сменить архив">{who}'
                f'<span class="acct-name">{label}</span>{icon("unfold_more")}</summary>'
                f'<div class="menu-pop acct-menu" role="menu">{"".join(items)}</div></details>')
        nav = manage = ""
        if links:
            dests = [d for d in NAV if d[1] != "comments" or (current or {}).get("comments_on", True)]
            nav = '<nav class="dest" aria-label="Разделы архива">' + "".join(
                f'<a class="dest-i{" on" if key == active else ""}" href="{E(getattr(links, route)())}"'
                f'{" aria-current=page" if key == active else ""}><span class="dest-ic">{icon(ic, 24, fill=key == active)}</span>'
                f'<span class="dest-t">{t}</span></a>' for route, key, t, ic in dests) + "</nav>"
            manage = icon_btn("tune", "Управление архивом", links.sync(), cls="on" if active in MANAGE else "")
        theme = (f'<button class="icon-btn theme-btn" type="button" title="Светлая или тёмная тема" aria-label="Сменить тему">'
                 f'{icon("dark_mode", cls="t-dark")}{icon("light_mode", cls="t-light")}</button>')
        return (f'<header class="appbar"><div class="appbar-in">{acct}{nav}<span class="sp"></span>'
                f'<a class="job-ind" href="/jobs" hidden>{RING}<span class="job-pct"></span></a>{manage}{theme}</div></header>')

    def page(self, title: str, body: str, *, current: dict | None, accounts: list[dict], active: str = "",
             wide: bool = False, extra: str = "", bare: bool = False) -> str:
        """`bare`: without the app bar (the page after LDTF was stopped: its links would lead nowhere)."""
        name = current["name"] if current else ""
        if current and current.get("guard") and active != "sync":   # the sync tab shows the full guard card
            body = guard_banner(current) + body
        full = E(title) + (f" — {E(name)}" if name and name != title else "")
        return (f'<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                f'<meta name="color-scheme" content="light dark"><meta name="theme-color" content="#f3f4f9">'
                f'<title>{full} · LDTF</title><link rel="icon" href="{Links.asset("brand/ldtf.svg")}" type="image/svg+xml">'
                f'<link rel="icon" href="{Links.asset("brand/ldtf-32.png")}" sizes="32x32" type="image/png">'
                f'<link rel="apple-touch-icon" href="{Links.asset("brand/ldtf-180.png")}">'
                f'<script>{THEME_JS}{MEDIA_JS}</script>'
                f'<link rel="stylesheet" href="{Links.asset("vendor/photoswipe/photoswipe.css")}">'
                f'<link rel="stylesheet" href="{Links.asset("style.css")}"></head>'
                f'<body>{"" if bare else self.appbar(current, accounts, active)}'
                f'<main class="page page--{"wide" if wide else "read"}">{body}</main>'
                f'{PAGE_TEMPLATES}'
                f'<script src="{Links.asset("vendor/photoswipe/photoswipe.umd.min.js")}"></script>'
                f'<script src="{Links.asset("vendor/photoswipe/photoswipe-lightbox.umd.min.js")}"></script>'
                f'<script src="{Links.asset("app.js")}"></script>{extra}</body></html>\n')

    def form(self, action: str, inner: str, cls: str = "", confirm: str | None = None, hidden: bool = False,
             fid: str = "", autosave: bool = False) -> str:
        """A POST form with the CSRF token; `confirm`: ask before sending; `hidden`: shown later by app.js;
        `fid`: the form's id (buttons elsewhere on the page submit it with form="...");
        `autosave`: settings - every control saves itself when changed (app.js), no "Save" button, and the browser
        never restores states of its own on "Back"."""
        c = f' data-confirm="{E(confirm)}"' if confirm else ""
        k = f' class="{cls}"' if cls else ""
        i = f' id="{E(fid)}"' if fid else ""
        a = ' data-autosave autocomplete="off"' if autosave else ""
        return (f'<form method="post" action="{E(action)}"{i}{k}{c}{a}{" hidden" if hidden else ""}>'
                f'<input type="hidden" name="_csrf" value="{E(self.csrf)}">{inner}</form>')


# ---------------------------------------------------------------------- comments
def cont_btn(n: int) -> str:
    """Shown by CSS only at the indentation limit (Reddit-style "continue this thread")."""
    return (f'<button class="c-cont">{icon("subdirectory_arrow_right")}Продолжить ветку · {n} '
            f'{plural(n, "ответ", "ответа", "ответов")}</button>')


KIDS_OPEN = '<div class="c-kids"><button class="c-line" title="Свернуть ветку" aria-label="Свернуть ветку"></button>'


def more_btn(n: int | None = None) -> str:
    return f'<button class="c-more">{icon("keyboard_arrow_down")}Развернуть ветку{f" · {n}" if n else ""}</button>'


class CommentView:
    """Renders comments (HTML for the app, Markdown lines for md/)."""

    def __init__(self, resolver: MediaResolver, local_posts: set[int], users: Any, uid: int, report: Report,
                 links: Links | None, rx: Reactions | None, md_root: str = "../../../"):
        self.resolver = resolver
        self.local_posts = local_posts
        self.users = users            # mapping: author id -> {name, avatar, ...}
        self.uid = uid
        self.report = report
        self.links = links
        self.rx = rx
        self.R = "/"
        self.md_R = md_root
        self.linker = Linker(local_posts, links.post if links else None)

    def ctx(self, cid: int) -> Ctx:
        return Ctx(self.resolver, self.linker, self.R, self.md_R, f"c:{cid}", f"comment {cid}", self.report,
                   self.local_posts)

    def user(self, aid: Any) -> dict:
        return self.users.get(aid) or {}

    def author_name(self, aid: Any) -> str:
        return self.user(aid).get("name") or (f"id{aid}" if aid else "аноним")

    def avatar_html(self, aid: Any) -> str:
        return avatar(avatar_src(self.resolver, self.user(aid).get("avatar")))

    def media_parts(self, c: dict) -> tuple[str, list[str], list[dict]]:
        ctx = self.ctx(c["id"])
        hs, mds, norms = [], [], []
        for m in c.get("media") or []:
            t = m.get("type") if isinstance(m, dict) else None
            try:
                if t in ("image", "movie"):
                    mi = media_info(m, self.resolver)
                    if not mi:
                        raise ValueError("нет uuid")
                    if t == "movie":
                        mi["kind"] = "video" if mi["kind"] == "image" else mi["kind"]
                    hs.append(media_html(mi, ctx))
                    mds.append(media_md(mi, ctx))
                    norms.append({"type": t, **(media_norm(mi) or {})})
                elif t == "video":
                    h, md, n = _video_parts(m, ctx)
                    hs.append(h)
                    mds.append(md)
                    norms.append({"type": t, **n})
                elif t in ("link", "osnovaEmbed"):   # a link card / a DTF post attached to the comment
                    h, md, n = _link_card(m.get("data") or {}, ctx) if t == "link" else b_osnova_embed({t: m}, ctx)
                    hs.append(h)
                    mds.append(md)
                    norms.append({"type": t, **n})
                else:
                    self.report.add("unsupported", f"comment-media:{t}", ctx.where)
                    hs.append(f'<div class="b-unsupported"><div class="u-head">{icon("warning")}Неподдерживаемое '
                              f'вложение: <code>{E(str(t))}</code></div>{json_spoiler(m)}</div>')
                    mds.append(f"> ⚠ вложение `{t}`\n\n```json\n{json.dumps(m, ensure_ascii=False, indent=2)}\n```")
                    norms.append({"type": t, "supported": False, "raw": m})
            except Exception as e:  # noqa: BLE001
                self.report.add("errors", f"comment-media:{t}", ctx.where, str(e)[:200])
                hs.append(f'<div class="b-unsupported"><div class="u-head">{icon("warning")}Вложение не отображено: '
                          f'{E(str(e))}</div>{json_spoiler(m)}</div>')
                norms.append({"type": t, "supported": False, "raw": m, "error": str(e)})
        return "".join(hs), mds, norms

    def one(self, c: dict, post_id: int | None, anchor: bool, toggle: int = 0, cls: str = "") -> str:
        mine = c.get("author") == self.uid
        name = E(self.author_name(c.get("author")))
        pid = post_id or (c.get("entry") or {}).get("id")
        date_link = (f'<a class="cd" href="{E(comment_url(pid, c["id"]))}"{EXT_LINK} '
                     f'title="Открыть на DTF">{ts_human(c.get("date"))}</a>') if pid else ts_human(c.get("date"))
        badges = []
        if c.get("rx") and self.rx:
            badges.append(self.rx.score_html(c["rx"], c.get("likes")))
        elif c.get("likes"):
            badges.append(f'<span class="score" title="Рейтинг DTF"><span class="pos">▲ {c["likes"]}</span></span>')
        rx_h = self.rx.html(c["rx"], self.R, f"comment {c['id']}") if c.get("rx") and self.rx else ""
        if c.get("donation"):
            badges.append(f'<span class="badge don" title="Донат автору поста вместе с комментарием">'
                          f'{icon("volunteer_activism")}Донат {rub(c["donation"])}</span>')
        hist = self.links.comment_history(c["id"]) if c.get("hist") and self.links else None
        if hist and not c.get("site"):   # the versions the archive keeps
            badges.append(f'<a class="c-ed c-hist" href="{E(hist)}" title="Изменён — история правок">{icon("edit")}'
                          f'история</a>')
        elif c.get("isEdited"):
            badges.append(f'<span class="c-ed" title="Отредактирован">{icon("edit")}</span>')
        if c.get("site"):
            tag, href = ("a", f' href="{E(hist)}"') if hist else ("span", "")
            badges.append(f'<{tag} class="c-site"{href} title="На DTF комментария больше нет; в архиве сохранён прежний текст">'
                          f'{icon("history")}{E(state_title(c["site"]))}</{tag}>')
        tog = ""
        if toggle:
            tog = (f'<button class="c-toggle" data-open="Показать ответы · {toggle}" '
                   f'data-close="Свернуть">Свернуть</button>')
        text = comment_text(c.get("text"), self.linker).html
        removed = ""
        if c.get("isRemoved") and not c.get("text"):
            text = "Комментарий удалён" + (" модератором" if c.get("removedByModerator") else "")
            removed = " removed"
        media_h = self.media_parts(c)[0] if c.get("media") else ""
        idattr = f' id="c{c["id"]}"' if anchor else ""
        if c.get("donation") and not (c.get("text") or "").strip() and not media_h:
            text, removed = "", removed + " c-don-only"   # a donation without words: the badge says it all
        foot = rx_h
        if c.get("donated"):
            foot = (f'<span class="rx don" title="Донаты этому комментарию">{icon("volunteer_activism")}'
                    f'+{rub(c["donated"])}</span>') + foot
        don = " c-don" if c.get("donation") else ""
        return (f'<div class="c{" mine" if mine else ""}{don}{removed}{" " + cls if cls else ""}"{idattr} data-id="{c["id"]}">'
                f'<div class="c-h"><span class="ca">{self.avatar_html(c.get("author"))}{name}</span>{date_link}'
                f'{"".join(badges)}{tog}</div>{f"<div class=c-t>{text}</div>" if text else ""}'
                f'{f"<div class=\"c-m pswp-gallery\">{media_h}</div>" if media_h else ""}'
                f'{f"<div class=c-f>{foot}</div>" if foot else ""}</div>')

    def tree(self, roots: list[int], by_id: dict[int, dict], children: dict[int, list[int]],
             post_id: int | None, anchors: bool) -> str:
        out: list[str] = []
        counts: dict[int, int] = {}

        def n_desc(cid: int) -> int:
            if cid not in counts:
                counts[cid] = sum(1 + n_desc(k) for k in children.get(cid, []) if k in by_id)
            return counts[cid]

        def rec(cid: int) -> None:
            c = by_id[cid]
            kids = [k for k in children.get(cid, []) if k in by_id]
            out.append('<div class="c-node">')
            out.append(self.one(c, post_id, anchors, toggle=len(kids)))
            if kids:
                out.append(cont_btn(n_desc(cid)))
                out.append(KIDS_OPEN)
                for k in kids:
                    rec(k)
                out.append("</div>" + more_btn(n_desc(cid)))
            out.append("</div>")

        for r in roots:
            if r in by_id:
                rec(r)
        return "".join(out)

    def md_line(self, c: dict) -> str:
        who = f"**{self.author_name(c.get('author'))}**" + (" (автор архива)" if c.get("author") == self.uid else "")
        text = comment_text(c.get("text")).md or ("*(удалён)*" if c.get("isRemoved") else "")
        mds = self.media_parts(c)[1] if c.get("media") else []
        extra = (" " + " ".join(mds)) if mds else ""
        if c.get("site"):
            extra += f" *({state_title(c['site'])})*"
        return f"{who} ({ts_human(c.get('date'))}): {text}{extra}".replace("\n", " ")


def md_tree(roots: list[int], by_id: dict, children: dict, cv: CommentView) -> list[str]:
    out: list[str] = []

    def rec(cid: int, depth: int) -> None:
        out.append("  " * depth + "- " + cv.md_line(by_id[cid]))
        for k in children.get(cid, []):
            if k in by_id:
                rec(k, depth + 1)
    for r in roots:
        if r in by_id:
            rec(r, 0)
    return out
