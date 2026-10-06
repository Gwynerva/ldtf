"""App-level pages (archives, adding a user, archive management: sync / settings, app settings, diagnostics)
and POST actions. Every form answers with a redirect (reloading a page never repeats an action); its outcome comes
with the next page view (App.flash, the FLASH mark on the page)."""

from __future__ import annotations

import html
import re
import shutil
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable

from .. import __version__
from .. import winintegration as win
from ..api import is_not_found
from ..guard import ACCEPTABLE, account_problem, site_summary, state_title
from ..guard import hint as guard_hint, title as guard_title
from ..http import FatalNetworkError
from ..media import MEDIA_MODE_TITLES, avatar_key, effective_media
from ..normalize import EXT_LINK
from ..reactions import Reactions, load_config, save_negative
from ..scheduler import human_when
from ..scope import SCOPE_TITLES, comment_footprint, comments_kept, describe, has_comment_data
from ..settings import CHOICES, DEFAULTS, LIMITS, clean_settings, log_changes, save_settings
from ..state import META_GUARD, META_LAST_SYNC, Archive, read_meta
from ..sync import status as status_text
from ..util import COMMENTS, FILES, POSTS, count_label, human_bytes, log, num, plural, short, ts_date, ts_human
from .icons import icon
from .blocks_page import blocks_card
from .jobs import ACTIVE, CANCELLED, ERROR, JOB_STATES, QUEUED, RUNNING_TITLES, STATE_ICONS
from .ui import (Links, app_tabs, archive_link, avatar, badge, banner, btn, empty_state, fold, hidden_input, icon_btn,
                 manage_tabs, menu, menu_item, page_head, sec_head, snackbar, stat)

if TYPE_CHECKING:
    from .server import App, Handler

E = html.escape
FLASH = "<!--flash-->"   # the outcome of the form that led to the page (server.Handler.html puts it here)
ISSUES = "https://github.com/Gwynerva/ldtf/issues"
JOB_BADGES = {"sync": "синхронизация", "render": "пересборка", "purge": "удаление комментариев"}


# ---------------------------------------------------------------------- archives
def archives_page(app: "App", notice: str = FLASH) -> str:
    rows = []
    for a in app.accounts():
        L = Links(a["nick"])
        last = ((read_meta(app.library / a["nick"], (META_LAST_SYNC,)) or {}).get(META_LAST_SYNC) or {}).get("finished")
        when = ""
        job = app.jobs.for_nick(a["nick"])
        if job and job.state in ACTIVE:
            state = badge("в очереди" if job.state == QUEUED else JOB_BADGES.get(job.kind, job.kind), "sync", "run")
        elif a.get("guard"):
            state = (f'<a class="badge err" href="{E(L.sync())}" title="{E((a["guard"] or {}).get("message") or "")}">'
                     f'{icon("gpp_maybe")}остановлено · нужна проверка</a>')
        else:
            nxt = app.next_run(a["nick"])
            txt = (f"след. {human_when(nxt)}" if nxt else
                   ("обновлён " + ts_date(last) if last else "не синхронизирован"))
            state = f'<span class="acc-state" title="{E("Последняя синхронизация: " + ts_human(last)) if last else ""}">{E(txt)}</span>'
            when = f'<span class="li-s acc-when">{E(txt)}</span>'   # phones: under the name (no room on the right)
        href, sub = archive_link(a)
        sync = app.shell.form(L.action("sync/start"), icon_btn("sync", "Синхронизировать", submit=True))
        more = menu("more_vert", "Ещё", menu_item("Синхронизация", "history", L.sync()) +
                    menu_item("Настройки", "tune", L.settings()))
        rows.append(f'<div class="li acc">{avatar(a.get("avatar"))}<div class="li-body">'
                    f'<a class="li-link li-t" href="{E(href)}">{E(a["name"])}</a>'
                    f'<span class="li-s">@{E(a["nick"])}</span><span class="li-s">{sub}</span>{when}</div>'
                    f'<div class="li-trail">{state}{sync}{more}</div></div>')
    add = btn("Добавить", "tonal", "person_add", href="/add")
    if rows:
        content = f'<div class="card list acc-list">{"".join(rows)}</div>'
    else:
        content = empty_state("inventory_2", "Архивов пока нет",
                              "Добавьте пользователя DTF — LDTF сохранит его посты, а если нужно, и комментарии с "
                              "медиафайлами.", btn("Добавить пользователя", ic="person_add", href="/add"))
    body = page_head("Архивы", actions=add if rows else "") + notice + content
    return app.page("Архивы", body, active="archives")


# ---------------------------------------------------------------------- add user
def _folder_name(prof: dict) -> str:
    raw = prof.get("nickname") or (prof.get("uri") or "").strip("/") or f"id{prof.get('id')}"
    name = re.sub(r"[^\w.\-]+", "-", raw).strip("-.") or f"id{prof.get('id')}"
    return name[:64]


def _resolve(app: "App", ident: str) -> dict:
    from ..api import Dtf
    client = app.net.client(timeout=20, attempts=2)   # shares the budget with running syncs
    try:
        return Dtf(client).subsite(ident)
    finally:
        client.close()


SCOPE_CHOICES = (
    ("all", "forum", "Посты и комментарии",
     "Посты, все комментарии пользователя на DTF с контекстом обсуждений и медиафайлы. Первая выгрузка активного "
     "пользователя — до пары часов, её можно прерывать."),
    ("posts", "article", "Только посты",
     "Посты с медиафайлами, реакциями и счётчиками — быстро и компактно. Комментарии можно добавить позже в "
     "настройках архива."),
)


def scope_pick(value: str = "all") -> str:
    """The choice made when an archive is created: what it keeps."""
    return ('<fieldset class="scope-pick"><legend>Что сохранять</legend>' + "".join(
        f'<label class="scope-opt"><input type="radio" name="scope" value="{v}"{" checked" if v == value else ""}>'
        f'<span class="so-ic">{icon(ic)}</span><span class="so-t"><b>{E(t)}</b><span>{E(d)}</span></span>'
        f'{icon("check_circle", cls="so-ck", fill=True)}</label>' for v, ic, t, d in SCOPE_CHOICES) + "</fieldset>")


def add_page(app: "App", value: str = "", preview: dict | None = None, error: str = "") -> str:
    form = app.shell.form("/add", hidden_input("action", "preview") +
                                  f'<div class="sbar">{icon("search")}<input name="user" value="{E(value)}" autofocus required '
                                  f'placeholder="Ник, id или ссылка на профиль" aria-label="Ник, id или ссылка на профиль DTF">'
                                  f'{icon_btn("arrow_forward", "Найти", submit=True)}</div>')
    pv = ""
    if preview:
        folder = _folder_name(preview)
        c = preview.get("counters") or {}
        img = avatar(avatar_key(preview.get("avatar"), 128), lazy=False)   # a live lookup: always from DTF
        exists = (app.library / folder).exists()
        gone = account_problem(preview)
        subs = c.get("subscribers") or 0
        desc = (preview.get("description") or "").strip()
        action = ""
        if exists:
            action = btn("Открыть архив", ic="inventory_2", href=Links(folder).home())
        elif gone:
            action = banner("err", "DTF сообщает, что этот аккаунт " + ("удалён" if gone == "deleted" else "заморожен")
                            + ": его посты и комментарии заменены заглушками, архивировать нечего.")
        pv = (f'<div class="pv card">{img}<div class="pv-b"><div class="pv-name">{E(preview.get("name") or "")}</div>'
              f'<div class="muted small">@{E(folder)} · id {preview.get("id")}</div>'
              f'<div class="chips"><span class="chip">{icon("person")}'
              f'{count_label(subs, "подписчик", "подписчика", "подписчиков")}</span>'
              f'<span class="chip">{icon("calendar_month")}на DTF с {ts_date(preview.get("created"))}</span></div>'
              f'{f"<p class=pv-desc>{E(desc)}</p>" if desc else ""}{action}</div></div>')
        if not (exists or gone):
            pv += app.shell.form("/add", hidden_input("action", "create") + hidden_input("user", preview.get("id"))
                                 + scope_pick() + f'<div class="create-row">{btn("Создать архив", ic="add")}</div>',
                                 cls="create-form")
    body = (page_head("Добавить пользователя", "Сохраняются только публичные данные с DTF.")
            + form + (banner("err", E(error)) if error else "") + pv)
    return app.page("Добавить пользователя", body, active="add")


def lookup_error(e: Exception) -> str:
    """Why a profile lookup failed, for the user (not the exception text)."""
    if is_not_found(e):
        return "Пользователь не найден на DTF — проверьте ник или ссылку."
    if isinstance(e, FatalNetworkError) and e.offline:
        return "Нет подключения к интернету — проверьте сеть и повторите."
    return f"DTF не ответил, попробуйте ещё раз чуть позже ({short(str(e), 140)})."


# ---------------------------------------------------------------------- archive management: sync
def guard_card(app: "App", nick: str, g: dict) -> str:
    """What stopped the sync, examples, and the user's choices."""
    L = Links(nick)
    kind = g.get("kind") or ""
    d = g.get("details") or {}
    items = []
    for x in (d.get("samples") or [])[:10]:
        sub = f'<span class="li-s">{E(x.get("post") or "")}</span>' if x.get("post") else ""
        items.append(f'<li><a href="{E(x.get("url") or "")}"{EXT_LINK}>{E(x.get("title") or "")}</a>{sub}</li>')
    more = ""
    if d.get("lost") and d["lost"] > len(items):
        more = f'<li class="muted">…и ещё {d["lost"] - len(items)}</li>'
    lst = f'<ul class="guard-list">{"".join(items)}{more}</ul>' if items else ""

    def act(action: str, label: str, kind_: str, ic: str) -> str:
        return app.shell.form(L.action("guard"), hidden_input("action", action) + btn(label, kind_, ic))
    acts = []
    if kind in ACCEPTABLE:
        acts.append(act("accept", "Продолжить и сохранить удалённое", "", "shield"))
    acts.append(act("retry", "Проверить снова", "tonal" if kind in ACCEPTABLE else "", "restart_alt"))
    when = f'<p class="muted small">Остановлено {E(ts_human(g["ts"]))}</p>' if g.get("ts") else ""
    return (f'<section class="card guard-card" role="alert"><div class="guard-h">{icon("gpp_maybe", 28, fill=True)}'
            f'<div><h2>Синхронизация остановлена: {E(guard_title(kind))}</h2>'
            f'<p>{E(g.get("message") or "")}</p></div></div>{lst}'
            f'<p class="guard-hint">{E(guard_hint(kind))}</p>'
            f'<div class="btn-row">{"".join(acts)}</div>{when}</section>')


def own_losses(site: dict | None) -> dict:
    """What disappeared from DTF of the user's own posts and comments (not other people's comments in threads)."""
    return {k: v for k, v in (site or {}).items() if k != "context"}


FULL_CHECK = ("Проверить всё заново? LDTF снова скачает все {what} и перепроверит медиафайлы — для активного "
              "пользователя это может занять несколько часов. Обычная синхронизация и так подхватывает новое и "
              "изменения последних недель.")


def sync_page(app: "App", nick: str) -> str:
    arch = app.archive(nick)
    assert arch is not None
    job = app.jobs.for_nick(nick)
    L = Links(nick)
    s = arch.settings()
    kept = comments_kept(s)
    st, last, media, guard, site, site_items, skipped = "", None, (0, 0), None, "", [], 0
    if arch.exists():
        try:
            st = status_text(arch)
            ls = arch.get_meta(META_LAST_SYNC) or {}
            last = ls.get("finished")
            skipped = ((ls.get("stats") or {}).get("media") or {}).get("skipped_by_mode") or 0
            site = site_summary(own_losses((ls.get("stats") or {}).get("site")))
            site_items = (ls.get("stats") or {}).get("site_items") or []
            guard = arch.get_meta(META_GUARD)
            media = arch.media_usage()
        finally:
            arch.close()
    running = bool(job and job.state in ACTIVE)
    if running:
        (state, ic), cls = JOB_STATES[job.state], job.state
        if job.state != QUEUED:
            state = RUNNING_TITLES.get(job.kind, state)
    elif job and job.kind == "sync" and job.state in (ERROR, CANCELLED) and not guard:   # the latest try (details below)
        state, ic, cls = ("Синхронизация не удалась" if job.state == ERROR else "Синхронизация остановлена — прогресс сохранён",
                          "error" if job.state == ERROR else "cancel", job.state)
    elif guard and last:   # the guard card above says what happened; here: what the archive has
        state, ic, cls = f"Последняя успешная синхронизация {ts_human(last)}", "history", ""
    elif last and not guard:
        state, ic, cls = f"Синхронизировано {ts_human(last)}", "check_circle", "done"
    else:
        state, ic, cls = "Архив ещё не синхронизирован", "schedule", ""
    nxt = None if running else app.next_run(nick)
    if cls in (ERROR, CANCELLED):
        when = ((f"Последняя успешная — {ts_human(last)}" if last else "Архив ещё не синхронизирован")
                + (f". Следующая попытка: {human_when(nxt)}" if nxt else ""))
    elif nxt:
        when = f"Следующая автосинхронизация: {human_when(nxt)}"
    elif not running and not guard and s["schedule"] != "off" and not app.settings().get("autosync", True):
        when = "Автосинхронизация приостановлена для всех архивов (настройки приложения)"
    else:
        when = ""
    changes = ""
    if site and not guard and not running:
        lst = "".join(
            f'<li><a href="{E(L.post(x["id"]) if x["kind"] == "post" else L.go(x["id"]))}">{E(x.get("title") or "(без текста)")}</a>'
            f'<span class="li-s">{E(state_title(x.get("state")))}{" · " + E(x["post"]) if x.get("post") else ""}</span></li>'
            for x in site_items)
        more = fold("Что пропало", f'<ul class="guard-list">{lst}</ul>', "site-list") if lst else ""
        changes = banner("warn", f"<b>Изменения на DTF</b> при последней синхронизации: пропало {E(site)}. "
                                 f"В архиве сохранены прежние версии с пометкой, что на DTF их больше нет.{more}", "shield")
    what = "посты и комментарии" if kept else "посты"
    more = menu("more_vert", "Ещё",
                app.shell.form(L.action("sync/start"), hidden_input("full", 1) + menu_item("Проверить всё заново", "fact_check"),
                               confirm=FULL_CHECK.format(what=what))
                + app.shell.form(L.action("render"), menu_item(
                    "Пересобрать страницы", "restart_alt",
                    title="Собрать страницы архива заново из уже скачанного, без интернета. Обычно не нужно: LDTF "
                          "делает это сам после синхронизации и после своего обновления.")))
    actions = (f'<div class="job-actions"{" hidden" if running else ""}>'
               + ("" if guard else app.shell.form(L.action("sync/start"), btn("Синхронизировать", ic="sync"))) + more + "</div>")
    stop = app.shell.form(L.action("sync/stop"), hidden_input("job", job.id if job else 0)
                          + btn("Остановить", "danger tonal", "stop_circle"), cls="job-stop",
                          hidden=not running or job.kind != "sync")
    acc = app.account(nick) or {}
    stats = ""
    if acc.get("built") or media[0]:
        c = acc.get("comments") or 0
        stats = (sec_head("Архив") + '<div class="stats sync-stats">'
                 + stat(num(acc.get("posts") or 0), plural(acc.get("posts") or 0, *POSTS))
                 + (stat(num(c), plural(c, *COMMENTS)) if acc.get("comments_on", kept) else "")
                 + stat(num(media[0]), plural(media[0], "медиафайл", "медиафайла", "медиафайлов"))
                 + stat(human_bytes(media[1]), "на диске") + "</div>")
        mode = effective_media(s["media"], s["scope"])
        note = ""
        if mode == "off":
            note = "Медиафайлы не скачиваются"
        elif mode == "posts" and kept and skipped:
            note = f"{count_label(skipped, *FILES)} из комментариев не скачиваются"
        if note:
            stats += (f'<p class="sync-note">{icon("info")}<span>{note} — так задано в '
                      f'<a href="{E(L.settings())}">настройках архива</a>. Их показывает DTF, пока они там есть.</span></p>')
    icons = '<template class="icons">' + "".join(f'<i data-n="{n}">{icon(n, fill=n in ("check_circle", "error", "cancel"))}</i>'
                                                 for n in STATE_ICONS) + "</template>"
    body = (page_head("Управление архивом") + manage_tabs(L, "sync") + FLASH + (guard_card(app, nick, guard) if guard else changes) +
            f'<div id="job-panel" class="card" data-job="{job.id if job else ""}" data-running="{int(running)}">'
            f'<div class="jp-head"><span class="jp-ic {cls}">{icon(ic, fill=cls == "done")}</span>'
            f'<div class="jp-tt"><b class="jp-state">{E(state)}</b><span class="jp-time">{E(when)}</span></div>'
            f'{stop}{actions}</div>'
            f'<div class="jp-net" hidden></div><div class="jp-stages"></div>'
            + fold("Журнал", '<pre class="jp-log"></pre>', "fold jp-logw", "" if job else " hidden") + "</div>"
            + stats + fold("Технические подробности", f'<pre class="status">{E(st) or "Архив ещё пуст."}</pre>') + icons)
    return app.page("Синхронизация", body, nick, active="sync")


# ---------------------------------------------------------------------- archive management: settings
def setting(label: str, hint: str, control: str, cls: str = "", tag: str = "label") -> str:
    """One settings row; `tag="div"` for a control with labels of its own (radio groups)."""
    h = f'<span class="field-h">{hint}</span>' if hint else ""
    return (f'<{tag} class="setting{" " + cls if cls else ""}"><span class="setting-t"><span class="field-l">{label}</span>'
            f'{h}</span>{control}</{tag}>')


def seg_radio(name: str, value: str, options: Any, label: str, disabled: bool = False) -> str:
    """Segmented choice on radio buttons: options = [(value, title)]."""
    off = " disabled" if disabled else ""
    return (f'<div class="seg seg-radio" role="radiogroup" aria-label="{E(label)}">' + "".join(
        f'<label data-v="{v}"><input type="radio" name="{name}" value="{v}"{" checked" if value == v else ""}{off}>'
        f'{icon("check", cls="ck")}{E(t)}</label>' for v, t in options) + "</div>")


def number(name: str, value: Any, limits: dict, step: str = "1") -> str:
    """A number field with the bounds the settings module enforces (settings.LIMITS)."""
    lo, hi = limits[name]
    return (f'<input type="number" name="{name}" value="{E(str(value))}" step="{step}" min="{lo:g}" max="{hi:g}" '
            f'inputmode="decimal">')


def switch(name: str, on: bool) -> str:
    return f'<input class="switch" type="checkbox" name="{name}" value="1"{" checked" if on else ""}>'


def group(ic: str, title: str, inner: str, cls: str = "") -> str:
    return f'<section class="card fgroup{" " + cls if cls else ""}"><h2>{icon(ic)}{E(title)}</h2>{inner}</section>'


def drop_confirm(app: "App", arch: Archive, values: dict) -> str:
    """Before an archive switches to posts only: what goes and how much space it frees; the card's form sends the
    switch itself (`values`) with the confirmation. "Отмена" reloads the page: it shows what is saved."""
    fp = comment_footprint(arch)
    what = describe(fp)
    lost = (f'<p class="cc-warn">{icon("warning", fill=True)}<span>{count_label(fp["lost"], *COMMENTS)} уже удалены на '
            f'DTF и сохранились только в этом архиве — после удаления их не будет нигде.</span></p>') if fp["lost"] else ""
    fields = "".join(hidden_input(k, v) for k, v in values.items()) + hidden_input("confirm_drop", "1")
    L = Links(arch.nick)
    return (f'<section class="card confirm-card" role="alertdialog" aria-labelledby="cc-h">'
            f'<div class="guard-h">{icon("delete", 28)}<div><h2 id="cc-h">Перейти на «Только посты»?</h2>'
            f'<p>Будут удалены {E(what)}. Освободится до {E(human_bytes(fp["bytes"]))}.</p></div></div>{lost}'
            f'<p class="guard-hint">Посты, их медиафайлы, реакции и профиль останутся. Если потом снова выбрать '
            f'«Посты и комментарии», LDTF скачает заново всё, что ещё есть на DTF.</p>'
            f'<div class="btn-row">' + app.shell.form(L.settings(), fields + btn("Удалить комментарии и переключить", "danger", "delete"))
            + btn("Отмена", "text", href=L.settings()) + "</div></section>")


SCHEDULE_TITLES = (("daily", "Каждый день"), ("interval", "По интервалу"), ("off", "Выключена"))


def form_view(s: dict) -> dict:
    """An archive's settings as its form shows them: "из постов" is the same as "все" for an archive of posts only
    (that choice is hidden there)."""
    return dict(s, media="all") if s["scope"] == "posts" and s["media"] == "posts" else dict(s)


def sched_note(app: "App", nick: str, mode: str) -> str:
    """Under the schedule: when the next autosync is, or that all of them are paused (sent again after each change)."""
    if mode != "off" and not app.settings().get("autosync", True):
        return banner("warn", 'Автосинхронизация приостановлена для всех архивов — включите её в '
                              '<a href="/app">настройках приложения</a>.', ic="pause_circle")
    nxt = app.next_run(nick)
    if nxt:
        return f'<p class="sched-next">{icon("schedule")}Следующая автосинхронизация: {E(human_when(nxt))}</p>'
    return ""


def settings_page(app: "App", nick: str, notice: str = FLASH, confirm: dict | None = None) -> str:
    """Every control saves itself when changed (app.js, one field at a time); `confirm`: the values sent without JS
    that switch the archive to posts only (the page asks first)."""
    arch = app.archive(nick)
    assert arch is not None
    s = form_view(dict(arch.settings(), **(clean_settings(confirm, arch.settings()) if confirm else {})))
    L = Links(nick)
    mode = s["schedule"]
    purging = app.jobs.purging(nick)
    scope_hint = ("Идёт удаление комментариев — режим можно будет сменить, когда оно закончится." if purging else
                  "При переходе на «Только посты» сохранённые комментарии удаляются — LDTF сначала покажет, сколько "
                  "их и сколько места освободится.")
    what = (setting("Материалы", scope_hint, seg_radio("scope", s["scope"], SCOPE_TITLES.items(), "Что сохранять",
                                                       disabled=purging), cls="seg-row scope-mode", tag="div")
            + setting("Медиафайлы", "Что не скачано, страницы показывают с DTF, пока файл там есть.",
                      seg_radio("media", s["media"], MEDIA_MODE_TITLES.items(), "Какие медиафайлы скачивать"),
                      cls="seg-row media-mode", tag="div"))
    sched = (f'<div class="sched">'
             + seg_radio("schedule", mode, SCHEDULE_TITLES, "Режим автосинхронизации")
             + setting("Каждые, часов", "", number("schedule_hours", s["schedule_hours"], LIMITS), "when-interval")
             + setting("Время", "Если компьютер в это время выключен, синхронизация пройдёт после запуска LDTF.",
                       f'<input type="time" name="schedule_time" value="{E(s["schedule_time"])}">', "when-daily")
             + f'<div data-region="sched">{sched_note(app, nick, mode)}</div></div>')
    inner = group("inventory_2", "Что сохранять", what, "what") + group("event_repeat", "Автосинхронизация", sched)
    danger = app.shell.form(
        L.action("delete"),
        f'<p>Удаляет папку архива со всеми данными. Медиафайлы, нужные другим архивам, остаются. Действие необратимо.</p>'
        f'<div class="form-row"><label class="field"><span class="field-l">Для подтверждения введите ник: {E(nick)}</span>'
        f'<input class="inp" name="confirm" autocomplete="off" spellcheck="false"></label>'
        f'{btn("Удалить архив", "danger", "delete")}</div>', cls="card fgroup danger-zone")
    top = drop_confirm(app, arch, confirm) if confirm else notice
    body = (page_head("Управление архивом") + manage_tabs(L, "settings") + f'<div data-region="notice">{top}</div>' +
            app.shell.form(L.settings(), inner, cls="settings", autosave=True) + blocks_card(arch) +
            f'<section class="danger-sec">{sec_head("Опасная зона")}{danger}</section>')
    return app.page("Настройки", body, nick, active="settings")


# ---------------------------------------------------------------------- app settings
def autostart_note(state: str) -> str:
    """What the switch alone can't say: Windows skips the entry, or it starts another copy of LDTF."""
    if state == "disabled":
        return banner("warn", "Windows не запускает LDTF: он выключен в списке автозагрузки Windows — «Параметры → "
                              "Приложения → Автозагрузка» или вкладка «Автозагрузка» диспетчера задач. Включите LDTF там.")
    if state == "elsewhere":
        exe = win.command_exe(win.autostart_value() or "")
        return banner("warn", f"С Windows сейчас запускается другая копия LDTF (<code>{E(exe)}</code>). Включите "
                              f"автозапуск здесь, чтобы запускалась эта.")
    return ""


def app_settings_page(app: "App", notice: str = FLASH) -> str:
    """Every control saves itself when changed (app.js, one field at a time)."""
    s = app.settings()
    tray = win.available()
    start = notify = ""
    if tray:
        state = win.autostart_state()
        # the shortcut buttons belong to a form of their own (form=...), the settings form has no buttons at all
        shortcut = lambda where, label: btn(label, "outlined sm", attrs=f' form="shortcuts" formaction="/app/shortcut?where={where}"')  # noqa: E731
        start = group("power_settings_new", "Запуск",
                      setting("Запускать вместе с Windows", "LDTF тихо стартует в трее после входа в систему; "
                              "синхронизации идут по расписанию.", switch("autostart", state in win.OWN_ENTRY))
                      + f'<div data-region="autostart">{autostart_note(state)}</div>'
                      + setting("Открывать браузер при запуске", "Когда LDTF запускают вручную.",
                                switch("open_browser", s["open_browser"]))
                      + '<div class="setting"><span class="setting-t"><span class="field-l">Ярлыки с иконкой LDTF</span>'
                        '<span class="field-h">Запуск без консольного окна.</span></span><span class="btn-row">'
                      + shortcut("desktop", "На рабочий стол") + shortcut("startmenu", "В меню Пуск") + "</span></div>")
        notify = setting("Уведомления в трее", "Когда закончилась синхронизация, запущенная вручную, и когда что-то "
                         "пошло не так.", switch("notify", s["notify"]))
    sync = group("sync", "Синхронизация",
                 setting("Автосинхронизация по расписанию", "Расписание задаётся в настройках каждого архива. "
                         "Выключите, чтобы приостановить все.", switch("autosync", s["autosync"])) + notify)
    form = app.shell.form("/app", start + sync, cls="settings", autosave=True)
    if tray:
        form += app.shell.form("/app/shortcut", "", fid="shortcuts")
    row = lambda label, hint, control="": (f'<div class="setting"><span class="setting-t"><span class="field-l">{label}</span>'  # noqa: E731
                                          f'<span class="field-h">{hint}</span></span>{control}</div>')
    about = (f'<section class="card fgroup"><h2>{icon("info")}О приложении</h2>'
             + row("Версия", f"LDTF {E(__version__)}")
             + row("Папка архивов", f"<code>{E(str(app.library))}</code>")
             + row("Совместимость с DTF", "Проверяет, не изменил ли DTF то, на что опирается синхронизация. "
                   "Пригодится, если синхронизации стали заканчиваться ошибками.",
                   btn("Проверить", "outlined sm", "network_check", href="/diagnostics"))
             + row("Остановить LDTF", "Идущие синхронизации сохранят прогресс и продолжатся при следующем запуске.",
                   app.shell.form("/app/quit", btn("Остановить", "danger tonal sm", "power_settings_new"),
                                  confirm="Остановить LDTF?"))
             + "</section>")
    body = page_head("Настройки приложения") + app_tabs("app") + notice + form + about
    return app.page("Настройки приложения", body, active="app")


def app_reactions_page(app: "App", notice: str = FLASH) -> str:
    """Which reactions count as dislikes ▼ — one list for every archive (reactions are the same for all posts on DTF)."""
    from ..normalize import MediaResolver
    rx = Reactions.for_library(app.library, MediaResolver.for_library(app.library), None)
    cells = []
    for row in rx.catalog():
        img = rx.img(row["id"], "/") or '<span class="rx-q">?</span>'
        neg = row["polarity"] == "negative"
        cells.append(f'<label class="rx-cell" title="Реакция #{row["id"]}"><input type="checkbox" name="neg" '
                     f'value="{E(str(row["id"]))}"{" checked" if neg else ""}>{img}<span>{E(row["label"] or "")}</span>'
                     f'{"<span class=rx-old>больше нет на сайте</span>" if row["retired"] else ""}'
                     f'<span class="rx-ck">{icon("check_circle", fill=True)}</span></label>')
    if cells:
        inner = (f'<p class="muted small rx-note">DTF считает любую реакцию как +1. Отметьте те, что LDTF считает '
                 f'дизлайками ▼, — рейтинги пересчитаются во всех архивах. Выбор сохраняется сразу.</p>'
                 f'<div class="rx-grid">{"".join(cells)}</div>')
        content = app.shell.form("/app/reactions", inner, autosave=True)
    else:
        content = empty_state("add_reaction", "Реакций пока нет",
                              "Каталог реакций появится после первой синхронизации любого архива.")
    body = page_head("Настройки приложения") + app_tabs("reactions") + notice + content
    return app.page("Реакции", body, active="reactions")


# ---------------------------------------------------------------------- diagnostics
def diagnostics_page(app: "App") -> str:
    d = app.diag
    rows = []
    for r in d.get("results", []):
        cls, ic = ("ok", "check_circle") if r["ok"] else (("warn", "warning") if r.get("warn") else ("bad", "error"))
        hint = ""
        if not r["ok"]:
            advice = ("Обычно это не мешает синхронизации." if r.get("warn") else
                      f'DTF изменил ответ — синхронизация может работать неправильно. Сообщите разработчику: '
                      f'<a href="{ISSUES}"{EXT_LINK}>issues на GitHub</a>.')
            dev = fold("Для разработчика", f'<p class="small">{E(r["hint"])}</p>', "fold dev") if r.get("hint") else ""
            hint = banner("err" if cls == "bad" else "warn", advice + dev)
        rows.append(f'<div class="li {cls}">{icon(ic, fill=True)}<div class="li-body"><span class="li-t">{E(r["name"])}</span>'
                    f'<span class="li-s">{E(r.get("detail") or "")}</span>{hint}</div></div>')
    run = app.shell.form("/diagnostics", btn("Проверить", ic="network_check"))
    running = d["state"] == "running"
    ok = sum(1 for r in d.get("results", []) if r["ok"])
    sub = ("Проверяет, не изменил ли DTF то, на что опирается синхронизация: адреса, страницы, форматы ответов, "
           "загрузку медиафайлов. Запустите, если синхронизации стали заканчиваться ошибками.")
    state = ""
    if running:
        state = '<div class="diag-run"><div class="lp ind"><div class="lp-i"></div></div><p class="muted small">Идёт проверка…</p></div>'
    elif d["state"] == "done":
        n = len(d.get("results", []))
        state = f'<p class="muted small">Проверено {ts_human(d.get("finished"))}: в порядке {ok} из {n}.</p>'
    body = (page_head("Совместимость с DTF", sub, run if not running else "") + state +
            f'<div class="card list diag" data-running="{int(running)}"{"" if rows else " hidden"}>{"".join(rows)}</div>')
    return app.page("Совместимость с DTF", body, active="diagnostics")


# ---------------------------------------------------------------------- POST
def form_values(f: dict) -> dict:
    """One value per field, without the CSRF token (settings forms)."""
    return {k: v[0] for k, v in f.items() if k != "_csrf"}


def saved(h: "Handler", back: str, note: str, values: dict | None = None, regions: dict | None = None,
          flash: str = "") -> None:
    """The answer to a change of settings. app.js gets JSON: what is saved now (the page sets every control to it) and
    fresh parts of the page by data-region; a form sent without JS goes back to the page with the outcome."""
    if h.wants_json():
        return h.json({"ok": True, "note": note, "values": values or {}, "regions": regions or {}})
    return h.redirect(back, flash=flash or snackbar(note))


def failed(h: "Handler", back: str, text: str) -> None:
    """A change that could not be made (app.js puts the control back and says why)."""
    if h.wants_json():
        return h.json({"error": text}, 500)
    return h.redirect(back, flash=banner("err", E(text)))


# ------------------------------------------------ app-level actions: handler(h, app, fields, val)
def post_add(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    ident = val("user")
    if not ident:
        return h.html(add_page(app, error="Введите ник, id или ссылку на профиль."))
    try:
        prof = _resolve(app, ident)
    except Exception as e:  # noqa: BLE001 - any failure of the lookup is shown to the user
        return h.html(add_page(app, ident, error=lookup_error(e)))
    if val("action") != "create":
        return h.html(add_page(app, ident, preview=prof))
    folder = _folder_name(prof)
    arch = Archive(app.library / folder, app.library)
    arch.state_dir.mkdir(parents=True, exist_ok=True)
    if not arch.settings_path.exists():
        scope = val("scope") if val("scope") in CHOICES["scope"] else DEFAULTS["scope"]
        save_settings(arch.settings_path, dict(DEFAULTS, scope=scope))
    app.jobs.submit(folder, "sync", user=str(prof["id"]), reason="create")
    app.invalidate()
    return h.redirect(Links(folder).sync())


def post_app_settings(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    """A change of the app settings: only the fields sent change (app.js sends the one control that changed, a switch
    as "1" / "0"); the autostart entry is written only when its own switch was changed."""
    values = form_values(f)
    note = "Сохранено"
    if "autostart" in values:
        want = values.pop("autostart").lower() in ("1", "true", "on", "yes")
        if win.available():
            try:
                now = win.set_autostart(want, source="настройки")
            except OSError as e:
                return failed(h, "/app", f"Не удалось изменить автозапуск: {e}")
            if now != want:
                return failed(h, "/app", "Windows не дал изменить автозапуск.")
            note = "Автозапуск включён" if want else "Автозапуск выключен"
    s = app.save_settings(values) if values else app.settings()
    app.invalidate()
    state = win.autostart_state()
    return saved(h, "/app", note, values={**s, "autostart": state in win.OWN_ENTRY},
                 regions={"autostart": autostart_note(state)})


def post_shortcut(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    where = (urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("where") or ["desktop"])[0]
    try:
        lnk = win.create_shortcut("startmenu" if where == "startmenu" else "desktop")
    except (OSError, ValueError) as e:
        return h.redirect("/app", flash=banner("err", f"Не удалось создать ярлык: {E(str(e))}"))
    return h.redirect("/app", flash=snackbar(f"Ярлык создан: {lnk}"))


def post_quit(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    app.request_quit()   # the server is stopping: this page is the last one it serves (no links to follow)
    return h.html(app.page("LDTF остановлен", empty_state(
        "power_settings_new", "LDTF остановлен", "Синхронизации сохранили прогресс. Запустите LDTF снова ярлыком "
        "или файлом LDTF.cmd.", tag="h1"), bare=True))


def post_diagnostics(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    if app.diag["state"] != "running":
        def run() -> None:
            from ..checkapi import run_checks
            app.diag.update(state="running", results=[], started=time.time())
            try:
                run_checks(on_result=lambda r: app.diag["results"].append(r), net=app.net)
            finally:
                app.diag.update(state="done", finished=time.time())
        threading.Thread(target=run, name="diagnostics", daemon=True).start()
    return h.redirect("/diagnostics")


def post_reactions(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    """The reactions counted as dislikes, for every archive: the whole set comes with every change (app.js adds an empty
    value, so "none" still sends the field). Pages switch at once; data/ and md/ of the archives are rebuilt in the
    background a few seconds after the last change."""
    old = load_config(app.library)["negative"]
    neg = save_negative(app.library, [x for x in f.get("neg", []) if x.strip()])
    log_changes("приложение", {"дизлайки": old}, {"дизлайки": neg})
    app.invalidate()
    app.rebuild_all_soon()
    return saved(h, "/app/reactions", "Сохранено — рейтинги пересчитаны, data/ и md/ обновятся в фоне",
                 values={"neg": [str(x) for x in neg]})


def post_agents_token(h: "Handler", app: "App", f: dict, val: Callable[..., str]) -> None:
    app.reset_mcp_token()
    return h.redirect("/app/agents", flash=snackbar("Выпущен новый токен — обновите настройки агентов"))


APP_ACTIONS = {"/add": post_add, "/app": post_app_settings, "/app/shortcut": post_shortcut, "/app/quit": post_quit,
               "/app/reactions": post_reactions, "/app/agents/token": post_agents_token,
               "/diagnostics": post_diagnostics}


# ------------------------------------------------ archive actions (/u/<nick>/<action>): handler(h, app, arch, fields, val)
def post_sync_start(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    app.jobs.submit(arch.nick, "sync", full=val("full") == "1", reason="manual")
    app.invalidate(arch.nick)
    return h.redirect(Links(arch.nick).sync())


def post_sync_stop(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    job_id = int(val("job") or 0)
    if job_id:
        app.jobs.cancel(job_id)
    return h.redirect(Links(arch.nick).sync())


def post_render(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    app.jobs.submit(arch.nick, "render", reason="manual")
    return h.redirect(Links(arch.nick).sync())


def post_guard(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    """The user's decision after the archive guard stopped a sync: accept the losses or check again."""
    nick, what = arch.nick, val("action")
    g = arch.get_meta(META_GUARD)
    if what == "accept" and g and g.get("kind") in ACCEPTABLE:
        app.jobs.submit(nick, "sync", accept=True, reason="manual")
    elif what == "retry":
        if g:
            arch.set_meta(META_GUARD, None)
            arch.commit()
            log.info(f"[app] @{nick}: проверка снова после остановки защитой ({g.get('kind')})")
        app.jobs.submit(nick, "sync", reason="manual")
    arch.close()
    app.invalidate()
    return h.redirect(Links(nick).sync())


def post_settings(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    """A change of the archive's settings: only the fields sent change (app.js sends the one control that changed).
    Switching to posts only drops the comments the archive has: first the user sees what goes (a confirmation card),
    then the purge runs as a job (its progress shows on the sync tab)."""
    nick, L = arch.nick, Links(arch.nick)
    values = form_values(f)
    confirmed = values.pop("confirm_drop", "") == "1"
    old = arch.settings()
    if app.jobs.purging(nick):   # the switch is locked while comments are being dropped
        values.pop("scope", None)
    if old["scope"] == "posts" and values.get("scope") == "all" and "media" not in values and old["media"] == "posts":
        values["media"] = "all"   # an archive of posts only showed "из постов" as "все": keep what the page showed
    new = clean_settings(values, old)
    if comments_kept(old) and not comments_kept(new) and has_comment_data(arch):
        if not confirmed:
            if h.wants_json():   # nothing is saved: the card's own form sends the switch with the confirmation
                return h.json({"confirm": drop_confirm(app, arch, {"scope": new["scope"]})})
            return h.html(settings_page(app, nick, confirm=values))
        save_settings(arch.settings_path, values)
        job = app.jobs.for_nick(nick)
        if job and job.state in ACTIVE and job.kind == "sync":   # it syncs comments that are about to go
            app.jobs.cancel(job.id)
        app.jobs.submit(nick, "purge", reason="manual")
        app.invalidate()
        log.info(f"[app] @{nick}: архив переключён на «Только посты», комментарии удаляются")
        return h.redirect(L.sync(), flash=snackbar("Архив хранит только посты — комментарии удаляются"))
    s = save_settings(arch.settings_path, values)
    app.invalidate()
    regions = {"sched": sched_note(app, nick, s["schedule"])}
    flash = ""
    if not comments_kept(old) and comments_kept(new):
        flash = regions["notice"] = banner(
            "info", "Комментарии загрузятся при следующей синхронизации — первая выгрузка активного пользователя "
                    "занимает до пары часов."
                    + app.shell.form(L.action("sync/start"), btn("Синхронизировать сейчас", "text", "sync")))
    return saved(h, L.settings(), "Сохранено", values=form_view(s), regions=regions, flash=flash)


def post_delete(h: "Handler", app: "App", arch: Archive, f: dict, val: Callable[..., str]) -> None:
    nick = arch.nick
    if val("confirm") != nick:
        return h.html(settings_page(app, nick, banner("err", "Ник для подтверждения введён неверно — архив не удалён.")))
    if app.jobs.busy(nick):
        return h.html(settings_page(app, nick, banner("err", "Сначала остановите синхронизацию этого архива.")))
    arch.close()
    app.invalidate(nick)  # drop cached views (they reference the archive's files)
    shutil.rmtree(app.library / nick, ignore_errors=True)
    if (app.library / nick).exists():
        return h.html(settings_page(app, nick, banner("err", "Часть файлов занята другим процессом и не удалена. "
                                                             "Закройте программы, открывшие архив, и повторите.")))
    gc = app.jobs.run_gc()   # the files no other archive needs
    app.invalidate()
    if gc is None or gc["status"] != "done":
        text = (f"Архив @{E(nick)} удалён. Ненужные ему медиафайлы удалятся из общего хранилища, когда закончатся "
                f"идущие синхронизации.")
    else:
        log.info(f"[app] архив {nick} удалён; освобождено медиа: {gc['files']} файлов, {human_bytes(gc['bytes'])}")
        text = (f"Архив @{E(nick)} удалён. Из общего хранилища удалено {count_label(gc['files'], *FILES)}, "
                f"освобождено {human_bytes(gc['bytes'])}.")
    return h.redirect("/archives", flash=banner("ok", text))


ARCHIVE_ACTIONS = {"sync/start": post_sync_start, "sync/stop": post_sync_stop, "render": post_render, "guard": post_guard,
                   "settings": post_settings, "delete": post_delete}
ARCHIVE_ACTION_RE = re.compile(r"/u/([^/]+)/(" + "|".join(map(re.escape, ARCHIVE_ACTIONS)) + ")")


def handle_post(h: "Handler", app: "App", path: str, f: dict, val: Callable[..., str]) -> None:
    if path in APP_ACTIONS:
        return APP_ACTIONS[path](h, app, f, val)
    m = ARCHIVE_ACTION_RE.fullmatch(path)
    if not m:
        return h.not_found()
    arch = app.archive(m.group(1))
    if arch is None:
        return h.not_found("Такого архива нет")
    return ARCHIVE_ACTIONS[m.group(2)](h, app, arch, f, val)
