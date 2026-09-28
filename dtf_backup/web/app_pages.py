"""App-level pages (archives, adding a user, archive management: sync / settings, app settings, diagnostics)
and POST actions."""

from __future__ import annotations

import html
import re
import shutil
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from .. import __version__
from ..guard import ACCEPTABLE, KIND_TITLES, site_summary
from ..scheduler import human_when
from ..settings import DEFAULTS, save_settings
from ..state import Archive, gc_media
from ..util import human_bytes, log, ts_date, ts_human, write_json
from .icons import icon
from .ui import (Links, avatar, banner, btn, counts_line, empty_state, icon_btn, manage_tabs, menu, menu_item, num,
                 page_head, plural, sec_head, snackbar, stat)

if TYPE_CHECKING:
    from .server import App, Handler

E = html.escape


# ---------------------------------------------------------------------- archives
def archives_page(app: "App", notice: str = "") -> str:
    rows = []
    for a in app.accounts():
        L = Links(a["nick"])
        arch = app.archive(a["nick"])
        last = None
        if arch and arch.exists():
            last = (arch.get_meta("last_sync") or {}).get("finished")
            arch.close()
        job = app.jobs.for_nick(a["nick"])
        if job and job.state in ("queued", "running"):
            state = f'<span class="badge run">{icon("sync")}{"синхронизация" if job.state == "running" else "в очереди"}</span>'
        elif a.get("guard"):
            state = (f'<a class="badge err" href="{E(L.sync())}" title="{E((a["guard"] or {}).get("message") or "")}">'
                     f'{icon("gpp_maybe")}остановлено · нужна проверка</a>')
        else:
            nxt = app.next_run(a["nick"])
            txt = (f"след. {human_when(nxt)}" if nxt else
                   ("обновлён " + ts_date(last) if last else "не синхронизирован"))
            state = f'<span class="acc-state" title="{E("Последняя синхронизация: " + ts_human(last)) if last else ""}">{E(txt)}</span>'
        sub = counts_line(a.get("posts") or 0, a.get("comments") or 0) if a.get("built") else "архив ещё собирается"
        sync = app.shell.form(L.base + "/sync/start", icon_btn("sync", "Синхронизировать", submit=True))
        more = menu("more_vert", "Ещё", menu_item("Синхронизация", "history", L.sync()) +
                    menu_item("Настройки", "tune", L.settings()) + menu_item("Реакции", "add_reaction", L.reactions()))
        rows.append(f'<div class="li acc">{avatar(a.get("avatar"))}<div class="li-body">'
                    f'<a class="li-link li-t" href="{E(L.home() if a.get("built") else L.sync())}">{E(a["name"])}</a>'
                    f'<span class="li-s">@{E(a["nick"])}</span><span class="li-s">{sub}</span></div>'
                    f'<div class="li-trail">{state}{sync}{more}</div></div>')
    add = btn("Добавить", "tonal", "person_add", href="/add")
    if rows:
        content = f'<div class="card list acc-list">{"".join(rows)}</div>'
    else:
        content = empty_state("inventory_2", "Архивов пока нет",
                              "Добавьте пользователя DTF — приложение сохранит его посты, комментарии и медиафайлы.",
                              btn("Добавить пользователя", ic="person_add", href="/add"))
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


def add_page(app: "App", value: str = "", preview: dict | None = None, error: str = "") -> str:
    form = app.shell.form("/add", f'<input type="hidden" name="action" value="preview">'
                                  f'<div class="sbar">{icon("search")}<input name="user" value="{E(value)}" autofocus required '
                                  f'placeholder="Ник, id или ссылка на профиль" aria-label="Ник, id или ссылка на профиль DTF">'
                                  f'{icon_btn("arrow_forward", "Найти", submit=True)}</div>')
    pv = ""
    if preview:
        folder = _folder_name(preview)
        c = preview.get("counters") or {}
        av = ((preview.get("avatar") or {}).get("data") or {}).get("uuid")
        img = avatar(f"https://leonardo.osnova.io/{av}/-/scale_crop/128x128/" if av else None, lazy=False)
        exists = (app.library / folder).exists()
        from ..guard import account_problem
        gone = account_problem(preview)
        action = (btn("Открыть архив", ic="inventory_2", href=Links(folder).home()) if exists else
                  banner("err", "DTF сообщает, что этот аккаунт " + ("удалён" if gone == "deleted" else "заморожен")
                         + ": его посты и комментарии заменены заглушками, архивировать нечего.") if gone else
                  app.shell.form("/add", f'<input type="hidden" name="action" value="create">'
                                         f'<input type="hidden" name="user" value="{E(str(preview.get("id")))}">'
                                         + btn("Создать архив", ic="add")))
        subs = c.get("subscribers") or 0
        desc = (preview.get("description") or "").strip()
        pv = (f'<div class="pv card">{img}<div class="pv-b"><div class="pv-name">{E(preview.get("name") or "")}</div>'
              f'<div class="muted small">@{E(folder)} · id {preview.get("id")}</div>'
              f'<div class="chips"><span class="chip">{icon("person")}{num(subs)} '
              f'{plural(subs, "подписчик", "подписчика", "подписчиков")}</span>'
              f'<span class="chip">{icon("calendar_month")}на DTF с {ts_date(preview.get("created"))}</span></div>'
              f'{f"<p class=pv-desc>{E(desc)}</p>" if desc else "<p></p>"}{action}</div></div>'
              + ("" if exists or gone else f'<p class="muted small">Первая выгрузка активного пользователя занимает от нескольких '
                                   f'минут до пары часов; её можно прерывать и продолжать.</p>'))
    body = (page_head("Добавить пользователя", "Посты, комментарии со всего сайта с контекстом обсуждений и медиафайлы. "
                                               "Только публичные данные.")
            + form + (banner("err", E(error)) if error else "") + pv)
    return app.page("Добавить пользователя", body, active="add")


# ---------------------------------------------------------------------- archive management: sync
STATUS_ICONS = ("check_circle", "progress_activity", "radio_button_unchecked", "cancel", "error", "block", "sync", "schedule",
                "gpp_maybe")
GUARD_HINTS = {
    "account-deleted": "Если аккаунт вернут, нажмите «Проверить снова». До тех пор архив остаётся как есть, а автосинхронизация "
                       "для него не запускается.",
    "account-frozen": "Заморозку могут снять — тогда нажмите «Проверить снова». До тех пор архив остаётся как есть, а "
                      "автосинхронизация для него не запускается.",
    "account-missing": "Возможно, аккаунт удалён или DTF временно отвечает с ошибками. Попробуйте «Проверить снова» позже.",
    "posts-mass": "Если посты удалили вы сами или их убрала модерация, нажмите «Продолжить»: они будут отмечены как удалённые на "
                  "DTF, а в архиве останутся сохранённые версии. Если это сбой DTF, проверьте снова позже.",
    "comments-mass": "Если комментарии удалили вы сами или модерация, нажмите «Продолжить»: они будут отмечены как удалённые на "
                     "DTF, а в архиве останутся сохранённые тексты. Если это сбой DTF, проверьте снова позже.",
}


def guard_card(app: "App", nick: str, g: dict) -> str:
    """What stopped the sync, examples, and the user's choices."""
    L = Links(nick)
    kind = g.get("kind") or ""
    d = g.get("details") or {}
    items = []
    for x in (d.get("samples") or [])[:10]:
        sub = f'<span class="li-s">{E(x.get("post") or "")}</span>' if x.get("post") else ""
        items.append(f'<li><a href="{E(x.get("url") or "")}" target="_blank" rel="noopener">{E(x.get("title") or "")}</a>{sub}</li>')
    more = ""
    if d.get("lost") and d["lost"] > len(items):
        more = f'<li class="muted">…и ещё {d["lost"] - len(items)}</li>'
    lst = f'<ul class="guard-list">{"".join(items)}{more}</ul>' if items else ""

    def act(action: str, label: str, kind_: str, ic: str) -> str:
        return app.shell.form(L.base + "/guard", f'<input type="hidden" name="action" value="{action}">' + btn(label, kind_, ic))
    acts = []
    if kind in ACCEPTABLE:
        acts.append(act("accept", "Продолжить и сохранить удалённое", "", "shield"))
    acts.append(act("retry", "Проверить снова", "tonal" if kind in ACCEPTABLE else "", "restart_alt"))
    arch = app.archive(nick)
    if arch and arch.settings().get("schedule") != "off":
        acts.append(act("pause", "Выключить автосинхронизацию", "text", "pause_circle"))
    when = f'<p class="muted small">Остановлено {E(ts_human(g["ts"]))}</p>' if g.get("ts") else ""
    return (f'<section class="card guard-card" role="alert"><div class="guard-h">{icon("gpp_maybe", 28, fill=True)}'
            f'<div><h2>Синхронизация остановлена: {E(KIND_TITLES.get(kind, "нужна проверка"))}</h2>'
            f'<p>{E(g.get("message") or "")}</p></div></div>{lst}'
            f'<p class="guard-hint">{E(GUARD_HINTS.get(kind, ""))}</p>'
            f'<div class="btn-row">{"".join(acts)}</div>{when}</section>')


def _media_stats(arch: Archive) -> tuple[int, int]:
    try:
        r = arch.db.execute("SELECT COUNT(*), COALESCE(SUM(b.size),0) FROM store.blob b WHERE b.sha256 IN "
                            "(SELECT r.sha256 FROM store.media_ref r WHERE r.key IN (SELECT key FROM main.media_use))"
                            ).fetchone()
        return r[0], r[1]
    except sqlite3.Error:
        return 0, 0


def sync_page(app: "App", nick: str) -> str:
    from ..sync import status as status_text
    arch = app.archive(nick)
    assert arch is not None
    job = app.jobs.for_nick(nick)
    L = Links(nick)
    st, last, media, guard, site, site_items = "", None, (0, 0), None, "", []
    if arch.exists():
        try:
            st = status_text(arch)
            ls = arch.get_meta("last_sync") or {}
            last = ls.get("finished")
            site = site_summary((ls.get("stats") or {}).get("site"))
            site_items = (ls.get("stats") or {}).get("site_items") or []
            guard = arch.get_meta("guard")
            media = _media_stats(arch)
        finally:
            arch.close()
    running = bool(job and job.state in ("queued", "running"))
    if running:
        state, ic, cls = ("В очереди", "schedule", "queued") if job.state == "queued" else ("Идёт синхронизация", "sync", "running")
    elif guard and last:   # the guard card above says what happened; here: what the archive has
        state, ic, cls = f"Последняя успешная синхронизация {ts_human(last)}", "history", ""
    elif guard:
        state, ic, cls = "Архив ещё не синхронизирован", "schedule", ""
    elif last:
        state, ic, cls = f"Синхронизировано {ts_human(last)}", "check_circle", "done"
    else:
        state, ic, cls = "Архив ещё не синхронизирован", "schedule", ""
    nxt = None if running else app.next_run(nick)
    when = f"Следующая автосинхронизация: {human_when(nxt)}" if nxt else ""
    changes = ""
    if site and not guard and not running:
        from ..guard import state_title
        lst = "".join(
            f'<li><a href="{E(L.post(x["id"]) if x["kind"] == "post" else L.go(x["id"]))}">{E(x.get("title") or "(без текста)")}</a>'
            f'<span class="li-s">{E(state_title(x.get("state")))}{" · " + E(x["post"]) if x.get("post") else ""}</span></li>'
            for x in site_items)
        more = (f'<details class="site-list"><summary>{icon("keyboard_arrow_down")}Что пропало</summary>'
                f'<ul class="guard-list">{lst}</ul></details>' if lst else "")
        changes = banner("warn", f"<b>Изменения на DTF</b> при последней синхронизации: пропало {E(site)}. "
                                 f"В архиве сохранены прежние версии с пометкой, что на DTF их больше нет.{more}", "shield")
    more = menu("more_vert", "Ещё",
                app.shell.form(L.base + "/sync/start", '<input type="hidden" name="full" value="1">'
                               + menu_item("Полная перепроверка", "fact_check")) +
                app.shell.form(L.base + "/render", menu_item("Пересобрать из скачанного", "restart_alt")))
    actions = (f'<div class="job-actions"{" hidden" if running else ""}>'
               + ("" if guard else app.shell.form(L.base + "/sync/start", btn("Синхронизировать", ic="sync"))) + more + "</div>")
    stop = app.shell.form(L.base + "/sync/stop", f'<input type="hidden" name="job" value="{job.id if job else 0}">'
                          + btn("Остановить", "danger tonal", "stop_circle"), cls="job-stop")
    acc = app.account(nick) or {}
    stats = ""
    if acc.get("built") or media[0]:
        c = acc.get("comments") or 0
        stats = (sec_head("Архив") + '<div class="stats sync-stats">'
                 + stat(num(acc.get("posts") or 0), plural(acc.get("posts") or 0, "пост", "поста", "постов"))
                 + stat(num(c), plural(c, "комментарий", "комментария", "комментариев"))
                 + stat(num(media[0]), plural(media[0], "медиафайл", "медиафайла", "медиафайлов"))
                 + stat(human_bytes(media[1]), "на диске") + "</div>")
    icons = '<template class="icons">' + "".join(f'<i data-n="{n}">{icon(n, fill=n in ("check_circle", "error", "cancel"))}</i>'
                                                 for n in STATUS_ICONS) + "</template>"
    body = (page_head("Управление архивом") + manage_tabs(L, "sync") + (guard_card(app, nick, guard) if guard else changes) +
            f'<div id="job-panel" class="card" data-job="{job.id if job else ""}" data-running="{int(running)}">'
            f'<div class="jp-head"><span class="jp-ic {cls}">{icon(ic, fill=cls == "done")}</span>'
            f'<div class="jp-tt"><b class="jp-state">{E(state)}</b><span class="jp-time">{E(when)}</span></div>'
            f'{stop if running else stop.replace("<form ", "<form hidden ", 1)}{actions}</div>'
            f'<div class="jp-stages"></div>'
            f'<details class="fold jp-logw"{" hidden" if not job else ""}><summary>{icon("keyboard_arrow_down")}Журнал</summary>'
            f'<pre class="jp-log"></pre></details></div>'
            + stats +
            f'<details class="fold"><summary>{icon("keyboard_arrow_down")}Технические подробности</summary>'
            f'<pre class="status">{E(st) or "Архив ещё пуст."}</pre></details>{icons}')
    return app.page("Синхронизация", body, nick, active="sync")


# ---------------------------------------------------------------------- archive management: settings
def setting(label: str, hint: str, control: str, cls: str = "") -> str:
    return (f'<label class="setting{" " + cls if cls else ""}"><span class="setting-t"><span class="field-l">{label}</span>'
            f'<span class="field-h">{hint}</span></span>{control}</label>')


def number(name: str, value: Any, step: str = "1", lo: int = 0, hi: int | None = None) -> str:
    mx = f' max="{hi}"' if hi is not None else ""
    return (f'<input type="number" name="{name}" value="{E(str(value))}" step="{step}" min="{lo}"{mx} '
            f'inputmode="decimal">')


def switch(name: str, on: bool) -> str:
    return f'<input class="switch" type="checkbox" name="{name}" value="1"{" checked" if on else ""}>'


def group(ic: str, title: str, inner: str, cls: str = "") -> str:
    return f'<section class="card fgroup{" " + cls if cls else ""}"><h2>{icon(ic)}{E(title)}</h2>{inner}</section>'


def settings_page(app: "App", nick: str, notice: str = "") -> str:
    arch = app.archive(nick)
    assert arch is not None
    s = arch.settings()
    L = Links(nick)
    mode = s["schedule"]
    seg = "".join(f'<label><input type="radio" name="schedule" value="{v}"{" checked" if mode == v else ""}>'
                  f'{icon("check", cls="ck")}{t}</label>'
                  for v, t in (("off", "Выключена"), ("interval", "Каждые N часов"), ("daily", "Ежедневно")))
    nxt = app.next_run(nick)
    paused = not app.settings().get("autosync", True)
    if paused and mode != "off":
        when = banner("warn", 'Автосинхронизация приостановлена для всех архивов — включите её в '
                              '<a href="/app">настройках приложения</a>.', ic="pause_circle")
    elif nxt:
        when = f'<p class="sched-next">{icon("schedule")}Следующая синхронизация: {E(human_when(nxt))}</p>'
    else:
        when = ""
    sched = (f'<div class="sched"><div class="seg seg-radio" role="radiogroup" aria-label="Режим">{seg}</div>'
             + setting("Интервал, часов", "Отсчёт от конца прошлой синхронизации.",
                       number("schedule_hours", s["schedule_hours"], lo=1, hi=168), "when-interval")
             + setting("Время", "Если компьютер был выключен, синхронизация пройдёт сразу после запуска LDTF.",
                       f'<input type="time" name="schedule_time" value="{E(s["schedule_time"])}">', "when-daily")
             + when + "</div>")
    inner = (group("event_repeat", "Автосинхронизация", sched) +
             group("storage", "Данные",
                   setting("Перепроверять за последние, дней", "Свежие ответы на комментарии и счётчики.",
                           number("refresh_days", s["refresh_days"], hi=3650))
                   + setting("Скачивать медиафайлы",
                             "Картинки, видео и аватарки — в общем хранилище без дублей.", switch("media", s["media"]))) +
             f'<div class="savebar"><span>Скорость и число одновременных синхронизаций — в '
             f'<a href="/app">настройках приложения</a>.</span>{btn("Сохранить")}</div>')
    danger = app.shell.form(
        L.base + "/delete",
        f'<p>Удаляет папку архива со всеми данными. Медиафайлы, нужные другим архивам, остаются. Действие необратимо.</p>'
        f'<div class="form-row"><label class="field"><span class="field-l">Для подтверждения введите ник: {E(nick)}</span>'
        f'<input class="inp" name="confirm" autocomplete="off" spellcheck="false"></label>'
        f'{btn("Удалить архив", "danger", "delete")}</div>',
        cls="card fgroup danger-zone", confirm=f"Удалить архив @{nick} безвозвратно?")
    body = (page_head("Управление архивом") + manage_tabs(L, "settings") + notice +
            app.shell.form(L.settings(), inner, cls="settings") +
            f'<section class="danger-sec">{sec_head("Опасная зона")}{danger}</section>')
    return app.page("Настройки", body, nick, active="settings")


# ---------------------------------------------------------------------- app settings
def app_settings_page(app: "App", notice: str = "") -> str:
    from .. import winintegration as win
    s = app.settings()
    start = ""
    if win.available():
        start = group("power_settings_new", "Запуск",
                      setting("Запускать вместе с Windows", "LDTF тихо стартует в трее после входа в систему; "
                              "синхронизации идут по расписанию.", switch("autostart", win.autostart_enabled()))
                      + setting("Открывать браузер при запуске", "Когда LDTF запускают вручную.",
                                switch("open_browser", s["open_browser"]))
                      + '<div class="setting"><span class="setting-t"><span class="field-l">Ярлыки с иконкой LDTF</span>'
                        '<span class="field-h">Запуск без консольного окна.</span></span><span class="btn-row">'
                      + btn("На рабочий стол", "outlined sm", attrs=' formaction="/app/shortcut?where=desktop"')
                      + btn("В меню Пуск", "outlined sm", attrs=' formaction="/app/shortcut?where=startmenu"')
                      + "</span></div>")
    else:
        start = f'<input type="hidden" name="open_browser" value="{"1" if s["open_browser"] else "0"}">'
    net = app.net.state()["api"]
    live = ""
    if net["fused"]:
        live = banner("err", f"DTF ограничил запросы: все синхронизации на паузе ещё {net['fused'] // 60 + 1} мин.")
    elif net["backoff"]:
        live = banner("warn", f"DTF просит подождать: пауза {net['backoff']} с, темп снижен до {net['rate']} запросов/с.")
    sync = group("sync", "Синхронизация",
                 setting("Автосинхронизация по расписанию", "Расписание задаётся в настройках каждого архива. "
                         "Выключите, чтобы приостановить все.", switch("autosync", s["autosync"]))
                 + setting("Одновременно архивов", "1–3. Все синхронизации делят общий лимит запросов.",
                           number("max_parallel", s["max_parallel"], lo=1, hi=3))
                 + setting("Запросов к DTF в секунду", "На всё приложение. Снижается сам, если DTF просит подождать.",
                           number("api_rate", s["api_rate"], "0.5", lo=1, hi=30))
                 + setting("Параллельных запросов к API", "1–12.", number("api_conn", s["api_conn"], lo=1, hi=12))
                 + setting("Параллельных загрузок медиа", "1–16.", number("media_conn", s["media_conn"], lo=1, hi=16))
                 + live)
    notify = group("notifications", "Уведомления",
                   setting("Уведомления в трее", "О завершённых и неудачных синхронизациях.",
                           switch("notify", s["notify"])))
    form = app.shell.form("/app", start + sync + notify + f'<div class="savebar"><span>Изменения применяются сразу, '
                                                          f'без перезапуска.</span>{btn("Сохранить")}</div>',
                          cls="settings")
    about = (f'<section class="card fgroup"><h2>{icon("info")}О приложении</h2>'
             + setting("Версия", "LDTF — локальный DTF.", f'<span class="muted">{E(__version__)}</span>')
             + f'<div class="setting"><span class="setting-t"><span class="field-l">Папка архивов</span>'
               f'<span class="field-h"><code>{E(str(app.library))}</code></span></span></div>'
             + f'<div class="setting"><span class="setting-t"><span class="field-l">Остановить LDTF</span>'
               f'<span class="field-h">Идущие синхронизации сохранят прогресс и продолжатся при следующем запуске.'
               f'</span></span>'
             + app.shell.form("/app/quit", btn("Остановить", "danger tonal sm", "power_settings_new"),
                              confirm="Остановить LDTF?")
             + "</div></section>")
    body = page_head("Настройки приложения") + notice + form + about
    return app.page("Настройки приложения", body, active="app")


# ---------------------------------------------------------------------- diagnostics
def diagnostics_page(app: "App") -> str:
    d = app.diag
    rows = []
    for r in d.get("results", []):
        cls, ic = ("ok", "check_circle") if r["ok"] else (("warn", "warning") if r.get("warn") else ("bad", "error"))
        hint = banner("err" if cls == "bad" else "warn", "Что делать: " + E(r["hint"])) if not r["ok"] and r.get("hint") else ""
        rows.append(f'<div class="li {cls}">{icon(ic, fill=True)}<div class="li-body"><span class="li-t">{E(r["name"])}</span>'
                    f'<span class="li-s">{E(r.get("detail") or "")}</span>{hint}</div></div>')
    run = app.shell.form("/diagnostics", btn("Проверить", ic="network_check"))
    running = d["state"] == "running"
    ok = sum(1 for r in d.get("results", []) if r["ok"])
    sub = "Проверяет, что DTF отвечает так, как ожидает приложение: эндпоинты, пагинация, форматы, CDN медиа."
    state = ""
    if running:
        state = '<div class="diag-run"><div class="lp ind"><div class="lp-i"></div></div><p class="muted small">Идёт проверка…</p></div>'
    elif d["state"] == "done":
        n = len(d.get("results", []))
        state = f'<p class="muted small">Проверено {ts_human(d.get("finished"))}: в порядке {ok} из {n}.</p>'
    body = (page_head("Диагностика API", sub, run if not running else "") + state +
            f'<div class="card list diag" data-running="{int(running)}"{"" if rows else " hidden"}>{"".join(rows)}</div>'
            f'<p class="muted small">Как устроено API DTF и где что адаптировать — в <code>docs/DTF_API.md</code>.</p>')
    return app.page("Диагностика API", body, active="diagnostics")


# ---------------------------------------------------------------------- POST
def handle_post(h: "Handler", app: "App", path: str, f: dict, val: Callable[..., str]) -> None:
    if path == "/add":
        ident = val("user")
        if not ident:
            return h.html(add_page(app, error="Введите ник, id или ссылку на профиль."))
        try:
            prof = _resolve(app, ident)
        except Exception as e:  # noqa: BLE001
            return h.html(add_page(app, ident, error=f"Не удалось найти пользователя: {e}"))
        if val("action") != "create":
            return h.html(add_page(app, ident, preview=prof))
        folder = _folder_name(prof)
        arch = Archive(app.library / folder, app.library)
        arch.state_dir.mkdir(parents=True, exist_ok=True)
        if not arch.settings_path.exists():
            save_settings(arch.settings_path, dict(DEFAULTS))
        app.jobs.submit(folder, "sync", user=str(prof["id"]), reason="create")
        app.invalidate(folder)
        return h.redirect(Links(folder).sync())

    if path == "/app":
        values = {k: v[0] for k, v in f.items() if k != "_csrf"}
        from .. import winintegration as win
        if win.available():
            want = values.pop("autostart", "") == "1"
            if want != win.autostart_enabled():
                try:
                    win.set_autostart(want)
                except OSError as e:
                    return h.html(app_settings_page(app, banner("err", f"Не удалось изменить автозапуск: {E(str(e))}")))
        app.save_settings(values)
        return h.html(app_settings_page(app, snackbar("Настройки сохранены")))
    if path == "/app/shortcut":
        from .. import winintegration as win
        import urllib.parse
        where = (urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("where") or ["desktop"])[0]
        try:
            lnk = win.create_shortcut("startmenu" if where == "startmenu" else "desktop")
        except (OSError, ValueError) as e:
            return h.html(app_settings_page(app, banner("err", f"Не удалось создать ярлык: {E(str(e))}")))
        return h.html(app_settings_page(app, snackbar(f"Ярлык создан: {lnk}")))
    if path == "/app/quit":
        app.request_quit()
        return h.html(app.page("LDTF остановлен", empty_state(
            "power_settings_new", "LDTF остановлен", "Синхронизации сохранили прогресс. Запустите LDTF снова ярлыком "
            "или файлом LDTF.cmd.", tag="h1")))

    if path == "/diagnostics":
        if app.diag["state"] != "running":
            def run() -> None:
                from ..checkapi import run_checks
                app.diag.update(state="running", results=[], started=time.time())
                try:
                    run_checks(on_result=lambda r: app.diag["results"].append(r), net=app.net)
                finally:
                    app.diag.update(state="done", finished=time.time())
            threading.Thread(target=run, daemon=True).start()
        return h.redirect("/diagnostics")

    m = re.fullmatch(r"/u/([^/]+)/(sync/start|sync/stop|render|settings|reactions|delete|guard)", path)
    if not m:
        return h.not_found()
    nick, action = m.group(1), m.group(2)
    arch = app.archive(nick)
    if arch is None:
        return h.not_found("Такого архива нет")
    L = Links(nick)
    if action == "sync/start":
        app.jobs.submit(nick, "sync", full=val("full") == "1", reason="manual")
        app.invalidate(nick)
        return h.redirect(L.sync())
    if action == "guard":
        what = val("action")
        g = arch.get_meta("guard")
        if what == "accept" and g and g.get("kind") in ACCEPTABLE:
            app.jobs.submit(nick, "sync", accept=True, reason="manual")
        elif what == "retry":
            if g:
                arch.set_meta("guard", None)
                arch.commit()
                log.info(f"[app] @{nick}: проверка снова после остановки защитой ({g.get('kind')})")
            app.jobs.submit(nick, "sync", reason="manual")
        elif what == "pause":
            save_settings(arch.settings_path, {**arch.settings(), "schedule": "off"})
        arch.close()
        app.invalidate(nick)
        return h.redirect(L.sync())
    if action == "sync/stop":
        job_id = int(val("job") or 0)
        if job_id:
            app.jobs.cancel(job_id)
        return h.redirect(L.sync())
    if action == "render":
        app.jobs.submit(nick, "render", reason="manual")
        return h.redirect(L.sync())
    if action == "settings":
        values = {k: v[0] for k, v in f.items() if k != "_csrf"}
        save_settings(arch.settings_path, values)
        return h.html(settings_page(app, nick, snackbar("Настройки сохранены")))
    if action == "reactions":
        from ..reactions import CONFIG_NAME, load_config
        cfg = load_config(arch.root)
        neg: list[Any] = []
        for x in f.get("neg", []):
            try:
                neg.append(int(x))
            except ValueError:
                neg.append(x)
        cfg["negative"] = neg
        write_json(arch.root / CONFIG_NAME, cfg)
        app.invalidate(nick)
        return h.redirect(L.reactions())
    if action == "delete":
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
        files, size = gc_media(app.library)
        log.info(f"[app] архив {nick} удалён; освобождено медиа: {files} файлов, {human_bytes(size)}")
        app.invalidate()
        return h.html(archives_page(app, banner("ok", f"Архив @{E(nick)} удалён. Из общего хранилища удалено {files} "
                                                      f"ненужных файлов ({human_bytes(size)}).")))
    return h.not_found()
