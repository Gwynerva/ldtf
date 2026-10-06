""""Блоки DTF" (/blocks): every block type of the DTF editor, how LDTF shows it, how often the archives have it, links
to real examples; and the block check of one archive (a card of its settings page): posts whose blocks LDTF shows
simplified or does not know (from the build: view.sqlite post_blocks, render-report.json blockIssues)."""

from __future__ import annotations

import html
import json
import urllib.parse as up
from typing import TYPE_CHECKING, Any

from ..blocks import TYPE_TITLES, Ctx, Report, block_level, json_spoiler, render_block
from ..blocksamples import demo_resolver, sample_block
from ..normalize import Linker
from ..state import Archive, archive_dirs, unpack
from ..util import count_label, num, short
from ..viewdb import open_view
from .icons import icon
from .ui import Links, badge, btn, empty_state, fold, page_head

if TYPE_CHECKING:
    from .server import App

E = html.escape
LEVELS = {"full": ("Полностью", "check_circle"), "generic": ("Упрощённо", "info"),
          "unsupported": ("Не поддерживается", "warning"), "error": ("Ошибка показа", "error")}
FILTERS = [("", "Все"), ("full", "Полностью"), ("generic", "Упрощённо"), ("unsupported", "Неизвестные"),
           ("used", "Есть в архивах")]
LEVEL_NOTES = {
    "full": "LDTF показывает блок полностью, как на сайте.",
    "generic": "LDTF показывает тексты, ссылки и медиа блока, без оформления сайта; исходные данные сохранены.",
    "unsupported": "Тип неизвестен LDTF: показывается карточка с исходными данными, ничего не теряется.",
}
POSTS = ("пост", "поста", "постов")
BLOCKS = ("блок", "блока", "блоков")


def _usage(app: "App", only: str = "") -> dict[str, dict]:
    """{type: {"n": blocks, "posts": n, "examples": [(nick, post_id, idx, title)]}} over the built archives."""
    out: dict[str, dict] = {}
    for d in archive_dirs(app.library):
        if only and d.name != only:
            continue
        db = open_view(Archive(d, app.library))
        if db is None:
            continue
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='post_blocks'").fetchone() is None:
                continue   # built by LDTF before 1.4: the next build adds it
            for t, n, np in db.execute("SELECT type, COUNT(*), COUNT(DISTINCT post_id) FROM post_blocks GROUP BY type"):
                u = out.setdefault(t, {"n": 0, "posts": 0, "examples": []})
                u["n"] += n
                u["posts"] += np
                if len(u["examples"]) < 5:
                    for pid, idx, title in db.execute(
                            "SELECT b.post_id, MIN(b.idx), p.title FROM post_blocks b JOIN posts p ON p.id=b.post_id "
                            "WHERE b.type=? GROUP BY b.post_id ORDER BY p.date DESC LIMIT ?", (t, 5 - len(u["examples"]))):
                        u["examples"].append((d.name, pid, idx, title or "Без заголовка"))
        finally:
            db.close()
    return out


def _real_block(app: "App", nick: str, pid: int, idx: int) -> tuple[dict | None, Any]:
    v = app.view(nick)
    if v is None:
        return None, None
    db = v.db()
    try:
        row = db.execute("SELECT raw FROM posts WHERE id=?", (pid,)).fetchone()
    finally:
        db.close()
    blocks = (unpack(row[0]).get("blocks") or []) if row else []
    return (blocks[idx] if 0 <= idx < len(blocks) else None), v


def page_blocks(app: "App", flt: str, only: str) -> str:
    nicks = [d.name for d in archive_dirs(app.library)]
    only = only if only in nicks else ""
    usage = _usage(app, only)
    types = list(TYPE_TITLES) + sorted(t for t in usage if t not in TYPE_TITLES)
    sample_ctx = Ctx(demo_resolver(), Linker(set(), None), "/", "/", "sample", "образец", Report())

    def link(**ch: Any) -> str:
        q = {"f": flt, "a": only}
        q.update(ch)
        q = {k: v for k, v in q.items() if v}
        return "/blocks" + ("?" + up.urlencode(q) if q else "")

    cards, toc = [], []
    for t in types:
        level = block_level(t)
        u = usage.get(t)
        if flt == "used" and not u or flt in LEVELS and level != flt:
            continue
        title = TYPE_TITLES.get(t) or "Неизвестный блок"
        name, ic = LEVELS[level]
        real = None
        if u and u["examples"]:
            nick, pid, idx, ptitle = u["examples"][0]
            block, v = _real_block(app, nick, pid, idx)
            if block is not None and v is not None:
                h, _, _ = render_block(block, v.ctx(f"post:{pid}", f"post {pid}"))
                real = (h, block, f'Настоящий блок из поста <a href="{E(Links(nick).post(pid))}#b{idx}">'
                                  f'{E(short(ptitle, 70))}</a>' + (f" (@{E(nick)})" if len(nicks) > 1 else ""))
        if real:
            shown, raw, label = real
        else:
            raw = sample_block(t)
            shown, _, _ = render_block(raw, sample_ctx)
            label = "Образец: так LDTF покажет этот блок" if t in TYPE_TITLES else "Образца нет"
        count = (f'<span class="bt-n">{icon("inventory_2")}{count_label(u["n"], *BLOCKS)} в '
                 f'{count_label(u["posts"], *POSTS)}</span>' if u else '<span class="bt-n muted">В архивах нет</span>')
        ex = ""
        if u and u["examples"]:
            ex = '<div class="bt-ex">' + "".join(
                f'<a class="chip" href="{E(Links(n).post(p))}#b{i}" title="{E(pt)}">{icon("article")}'
                f'<span>{E(short(pt, 40))}</span></a>' for n, p, i, pt in u["examples"]) + "</div>"
        cards.append(
            f'<section class="card bt bt-{level}" id="t-{E(t)}"><div class="bt-h"><div class="bt-t"><h2>{E(title)}</h2>'
            f'<code>{E(t)}</code></div>{badge(name, ic, "bt-lv " + level)}</div>'
            f'<p class="bt-note">{E(LEVEL_NOTES[level])}</p>'
            f'<div class="bt-sample"><div class="bt-label">{label}</div><div class="bt-body">{shown}</div></div>'
            f'<div class="bt-foot">{count}{ex}'
            f'{fold("Исходные данные блока", json_spoiler(raw, "JSON"), "fold bt-json")}</div></section>')
        toc.append(f'<a class="chip" href="#t-{E(t)}">{icon(ic, cls="lv-" + level)}{E(TYPE_TITLES.get(t) or t)}'
                   f'{f"<span class=n>{num(u["n"])}</span>" if u else ""}</a>')

    def chip(label: str, on: bool, href: str) -> str:
        return f'<a class="chip{" on" if on else ""}" href="{E(href)}">{icon("check", cls="ck") if on else ""}{E(label)}</a>'
    filters = '<div class="chips scroll filters">' + "".join(chip(lb, flt == k, link(f=k or None)) for k, lb in FILTERS)
    if len(nicks) > 1:
        filters += ('<span class="chips-div"></span>' + chip("Все архивы", not only, link(a=None))
                    + "".join(chip("@" + n, only == n, link(a=n)) for n in nicks))
    filters += "</div>"
    used = sum(1 for t in types if t in usage)
    sub = ("Как LDTF показывает каждый блок редактора DTF. Где в архивах блок уже есть, показан настоящий; "
           "иначе — образец." + (f" В архивах встречается типов: {used}." if usage else ""))
    body = page_head("Блоки DTF", sub) + filters
    if not cards:
        body += empty_state("widgets", "Таких блоков нет", "Уберите фильтр, чтобы увидеть все.")
    else:
        body += f'<nav class="chips scroll bt-toc" aria-label="Типы блоков">{"".join(toc)}</nav>' + "".join(cards)
    return app.page("Блоки DTF", body, active="blocks", wide=True)


# ---------------------------------------------------------------------- one archive: settings card
def archive_issues(arch: Archive) -> list[dict] | None:
    """Blocks of the archive's posts shown simplified, unknown or failed: [{post, idx, type, level}] (None: not built)."""
    rep = arch.report_path
    try:
        data = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
    except (OSError, ValueError):
        data = {}
    if "blockIssues" in data:
        return list(data["blockIssues"])
    db = open_view(arch)
    if db is None:
        return None
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='post_blocks'").fetchone() is None:
            return None
        return [{"post": r[0], "idx": r[1], "type": r[2], "level": r[3]} for r in
                db.execute("SELECT post_id, idx, type, level FROM post_blocks WHERE level != 'full'")]
    finally:
        db.close()


def blocks_card(arch: Archive) -> str:
    issues = archive_issues(arch)
    if issues is None:
        return ""
    L = Links(arch.nick)
    head = f'<h2>{icon("widgets")}Проверка блоков</h2>'
    catalog = btn("Все блоки DTF", "text sm", "widgets", href=f"/blocks?a={up.quote(arch.nick)}")
    if not issues:
        return (f'<section class="card fgroup bcheck">{head}<p class="bc-ok">{icon("check_circle", fill=True)}'
                f'<span>Все блоки постов этого архива LDTF показывает полностью.</span></p>{catalog}</section>')
    db = open_view(arch)
    titles: dict[int, str] = {}
    if db is not None:
        try:
            titles = {r[0]: r[1] or "Без заголовка" for r in db.execute("SELECT id, title FROM posts")}
        finally:
            db.close()
    by_post: dict[int, list[dict]] = {}
    for x in issues:
        by_post.setdefault(x["post"], []).append(x)
    rows = []
    for pid, xs in by_post.items():
        kinds = {}
        for x in xs:
            kinds.setdefault(x["type"], x["level"])
        chips = "".join(badge(TYPE_TITLES.get(t) or t, LEVELS.get(lv, LEVELS["unsupported"])[1], "bt-lv " + lv)
                        for t, lv in kinds.items())
        rows.append(f'<div class="li bc-li"><div class="li-body"><a class="li-link li-t" '
                    f'href="{E(L.post(pid))}#b{xs[0]["idx"]}">{E(titles.get(pid, f"Пост {pid}"))}</a>'
                    f'<span class="bc-kinds">{chips}</span></div></div>')
    first, rest = rows[:8], rows[8:]
    more = fold(f"Ещё {count_label(len(rest), *POSTS)}", f'<div class="list">{"".join(rest)}</div>', "fold") if rest else ""
    errors = sum(1 for x in issues if x["level"] == "error")
    note = (f'{count_label(len(by_post), *POSTS)} с блоками, которые LDTF показывает упрощённо или не знает'
            + (f"; не удалось показать: {errors}" if errors else "") +
            ". Исходные данные этих блоков сохранены — ссылки ведут прямо к блоку.")
    return (f'<section class="card fgroup bcheck">{head}<p>{E(note)}</p>'
            f'<div class="list bc-list">{"".join(first)}</div>{more}{catalog}</section>')

