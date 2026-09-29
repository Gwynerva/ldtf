"""Live contract checks: does DTF still answer the way this tool expects?  (see docs/DTF_API.md)

Used by `python -m dtf_backup check-api`, the app's "Диагностика API" page and tests/test_live_api.py.
Nothing here depends on fixed ids: a post with many comments and a nested comment are discovered at run time.
Every failed check carries a hint: which module to adapt.
"""

from __future__ import annotations

import http.client
import time
from typing import Any, Callable

from .api import MEDIA, Dtf
from .blocks import FULL, GENERIC_TYPES, PARTIAL
from .http import AdaptiveLimiter, HttpClient
from .media import avatar_key, collect_media, raw_key
from .normalize import REDIRECT_RE, unwrap_url

KNOWN_BLOCKS = set(FULL) | set(PARTIAL) | set(GENERIC_TYPES)
FAR = Dtf.FAR_ID


class Check:
    def __init__(self, key: str, name: str, hint: str, fn: Callable[[dict], str]):
        self.key, self.name, self.hint, self.fn = key, name, hint, fn


class Warn(Exception):
    """Not broken, but worth a look (e.g. a new editor block type)."""


class NoData(Exception):
    """A check needs what an earlier check should have found (that one failed)."""


class Ctx(dict):
    """Results shared between checks; a missing one is NoData, not a KeyError (those mean the API changed)."""

    def __missing__(self, key: str) -> Any:
        raise NoData(key)


def _ctx(user: str, net: Any = None) -> Ctx:
    if net is not None:  # inside the app: share the network budget with running syncs
        client = net.client(timeout=30, attempts=2)
    else:
        client = HttpClient({"api.dtf.ru": AdaptiveLimiter("api", 2, rate=5, fuse=3, base_backoff=3),
                             "*": AdaptiveLimiter("media", 2, fuse=3, base_backoff=3)}, timeout=30, attempts=2)
    return Ctx(user=user, client=client, api=Dtf(client))


# ---------------------------------------------------------------------- checks
def c_subsite(x: dict) -> str:
    p = x["api"].subsite(x["user"])
    assert isinstance(p.get("id"), int), "нет числового id"
    assert p.get("name"), "нет name"
    assert ((p.get("avatar") or {}).get("data") or {}).get("uuid"), "нет avatar.data.uuid"
    assert isinstance(p.get("created"), int), "нет created (unix time)"
    x["prof"] = p
    return f"id {p['id']}, {p['name']}"


def c_timeline(x: dict) -> str:
    uid = x["prof"]["id"]
    p1, cur = x["api"].timeline_page(uid)
    assert p1, "пустая первая страница"
    assert all(isinstance(it.get("blocks"), list) and it.get("id") for it in p1), "у постов нет id/blocks"
    assert cur, "нет cursor для следующей страницы"
    p2, _ = x["api"].timeline_page(uid, cur)
    overlap = {i["id"] for i in p1} & {i["id"] for i in p2}
    assert not overlap, f"вторая страница повторяет первую: {sorted(overlap)[:3]}"
    x["posts"] = p1 + p2
    return f"{len(p1)} + {len(p2)} постов, cursor работает"


def c_content(x: dict) -> str:
    posts = sorted(x["posts"], key=lambda p: -(p.get("counters") or {}).get("comments", 0))
    pid = posts[0]["id"]
    p = x["api"].content(pid)
    assert isinstance(p.get("blocks"), list) and p["blocks"], "нет blocks"
    x["post"] = p
    types = {b.get("type") for b in p["blocks"] if isinstance(b, dict)}
    for q in x["posts"]:
        types |= {b.get("type") for b in q.get("blocks") or [] if isinstance(b, dict)}
    new = sorted(t for t in types if t not in KNOWN_BLOCKS)
    if new:
        raise Warn(f"новые типы блоков: {', '.join(map(str, new))} — они сохраняются как есть и показываются "
                   f"плашкой; добавьте рендер в blocks.py")
    return f"пост {pid}: {len(p['blocks'])} блоков, типы известны ({', '.join(sorted(map(str, types)))})"


def c_tree(x: dict) -> str:
    p = x["post"]
    counter = (p.get("counters") or {}).get("comments", 0)
    items = x["api"].post_comments(p["id"])
    assert all(("id" in c and "replyTo" in c and "level" in c and "author" in c) for c in items), \
        "у комментариев нет id/replyTo/level/author"
    if counter:
        assert len(items) >= 0.9 * counter, f"получено {len(items)} из {counter} по счётчику (неполное дерево)"
    depth = max((c.get("level") or 0 for c in items), default=0)
    if counter > 20:
        assert depth >= 2, "в дереве нет уровней глубже 1 (похоже на firstLoad-режим)"
    x["tree"] = items
    return f"{len(items)} комментариев (счётчик {counter}), глубина до {depth}"


def c_branch(x: dict) -> str:
    deep = [c for c in x.get("tree", []) if (c.get("level") or 0) >= 2]
    assert deep, "не нашлось вложенного комментария для проверки"
    c = deep[0]
    items = x["api"].comment_branch(c["id"])
    ids = {i["id"] for i in items}
    assert c["id"] in ids, "в ветке нет самого комментария"
    assert c["replyTo"] in ids, "в ветке нет родителя комментария"
    return f"ветка вокруг {c['id']}: {len(items)} комментариев, предки на месте"


def c_feed(x: dict) -> str:
    uid = x["prof"]["id"]
    a, lid, lsv = x["api"].user_comments_page(uid)
    assert a, "пустая лента комментариев"
    assert all((c.get("author") or {}).get("id") == uid for c in a), "в ленте чужие комментарии"
    assert all((c.get("entry") or {}).get("id") for c in a), "нет entry.id у комментариев"
    b, _, _ = x["api"].user_comments_page(uid, lid, lsv)
    if b:
        assert not ({c["id"] for c in a} & {c["id"] for c in b}), "страницы ленты повторяются"
        assert max(c["date"] for c in b) <= min(c["date"] for c in a), "даты на второй странице не старше первой"
    return f"{len(a)} + {len(b)} комментариев, пагинация lastId/lastSortingValue работает"


def c_from_date(x: dict) -> str:
    uid = x["prof"]["id"]
    t = int(time.time()) - 60 * 86400
    items, _, _ = x["api"].user_comments_page(uid, FAR, t)
    assert all(c["date"] <= t for c in items), "старт ленты с даты (lastId=999999999) не работает"
    return f"старт с даты: {len(items)} комментариев старше {time.strftime('%d.%m.%Y', time.localtime(t))}"


def c_assets(x: dict) -> str:
    a = x["api"].assets()
    rx = a.get("reactions") or []
    assert rx and all("id" in r and r.get("staticUuid") for r in rx), "нет reactions[].id/staticUuid"
    x["reactions"] = rx
    return f"реакций {len(rx)}, значков {len(a.get('badges') or [])}"


def _get(x: dict, url: str, rng: str | None = "bytes=0-1023") -> tuple[int, str]:
    r = x["client"].fetch(url, {"Range": rng} if rng else {})
    return r.status, (r.headers.get("content-type") or "").split(";")[0]


def c_cdn_image(x: dict) -> str:
    keys = [k for k, _, kind in collect_media(x["posts"]) if kind in ("jpg", "png", "webp") and not k.startswith("http")]
    assert keys, "в постах не нашлось картинок"
    st, ct = _get(x, f"{MEDIA}/{keys[0]}/")
    assert st in (200, 206) and ct.startswith("image/"), f"ожидалась картинка, получено {st} {ct}"
    return f"{keys[0]}: {ct}"


def c_cdn_mp4(x: dict) -> str:
    keys = [k for k, _, kind in collect_media(x["posts"] + x.get("tree", [])) if kind in ("gif", "mp4")]
    if not keys:
        raise Warn("в выборке нет gif/видео — проверка пропущена")
    st, ct = _get(x, f"{MEDIA}/{keys[0]}/")
    assert ct == "video/mp4", f"для gif ожидалось перенаправление на mp4, получено {st} {ct}"
    return f"{keys[0]} → video/mp4"


def c_cdn_raw(x: dict) -> str:
    anim = [r for r in x.get("reactions", []) if r.get("animatedUuid")]
    if not anim:
        raise Warn("нет анимированных реакций — проверка пропущена")
    st, ct = _get(x, raw_key(anim[0]["animatedUuid"]) or "")
    assert ct in ("image/webp", "image/gif", "video/mp4", "image/png"), f"-/format/raw/ вернул {st} {ct}"
    return f"реакция #{anim[0]['id']}: {ct}"


def c_cdn_avatar(x: dict) -> str:
    key = avatar_key(x["prof"].get("avatar"))
    assert key, "нет аватара"
    st, ct = _get(x, key, None)
    assert st == 200 and ct.startswith("image/"), f"scale_crop вернул {st} {ct}"
    return f"72×72: {ct}"


def c_short_url(x: dict) -> str:
    pid = x["post"]["id"]
    conn = http.client.HTTPSConnection("dtf.ru", timeout=20)
    try:
        conn.request("GET", f"/{pid}", headers={"User-Agent": "Mozilla/5.0"})
        r = conn.getresponse()
        loc = r.getheader("Location") or ""
        r.read()
    finally:
        conn.close()
    assert r.status in (301, 302, 308) and f"/{pid}" in loc, f"dtf.ru/{pid} → {r.status} {loc}"
    return f"dtf.ru/{pid} → {r.status} {loc[:80]}"


def c_redirect(x: dict) -> str:
    import re
    for p in x["posts"] + [x["post"]]:
        for b in p.get("blocks") or []:
            for m in re.finditer(r'href="([^"]+)"', str((b.get("data") or {}).get("text") or "")):
                u = m.group(1).replace("&amp;", "&")
                if REDIRECT_RE.match(u):
                    out = unwrap_url(u)
                    assert out.startswith("http") and "api.dtf.ru" not in out, f"не разворачивается: {u}"
                    return f"{u[:60]}… → {out[:60]}"
            link = ((b.get("data") or {}).get("link") or {}).get("data") or {}
            if REDIRECT_RE.match(link.get("url") or ""):
                out = unwrap_url(link["url"])
                assert out.startswith("http") and "api.dtf.ru" not in out, "не разворачивается ссылка-карточка"
                return f"карточка → {out[:60]}"
    raise Warn("в выборке нет обёрнутых ссылок — проверка пропущена")


def c_keepalive(x: dict) -> str:
    lim = x["client"].limiters["api.dtf.ru"]
    before = lim.failures
    t = time.time()
    for _ in range(5):
        x["api"].subsite(x["user"])
    assert lim.failures == before, "ошибки соединения на серии запросов"
    return f"5 запросов подряд за {time.time() - t:.1f} с без ошибок"


CHECKS = [
    Check("subsite", "Профиль: subsite", "api.Dtf.subsite (версия v2.7, параметры uri/id)", c_subsite),
    Check("timeline", "Посты: timeline + cursor", "api.Dtf.timeline_page; sync.stage_posts", c_timeline),
    Check("content", "Пост целиком: content, типы блоков", "api.Dtf.content; blocks.py (новые типы блоков)", c_content),
    Check("tree", "Дерево комментариев поста (contentId)", "api.Dtf.post_comments (без firstLoad!)", c_tree),
    Check("branch", "Ветка вокруг комментария (commentId)", "api.Dtf.comment_branch; sync.stage_threads", c_branch),
    Check("feed", "Лента комментариев пользователя (subsiteId)", "api.Dtf.user_comments_page; sync.stage_comments", c_feed),
    Check("from_date", "Лента с произвольной даты", "sync._make_slices / stage_comments (срезы по месяцам)", c_from_date),
    Check("assets", "Каталог реакций: assets", "api.Dtf.assets; reactions.py", c_assets),
    Check("cdn_image", "CDN: картинка по uuid", "media.Downloader, api.media_url", c_cdn_image),
    Check("cdn_mp4", "CDN: gif → mp4", "media.Downloader (редиректы), normalize.media_info", c_cdn_mp4),
    Check("cdn_raw", "CDN: -/format/raw/ (анимированные реакции)", "media.raw_key, reactions.Reactions.srcs", c_cdn_raw),
    Check("cdn_avatar", "CDN: аватар -/scale_crop/72x72/", "media.avatar_key", c_cdn_avatar),
    Check("short_url", "Короткие ссылки dtf.ru/<id>", "api.post_url / comment_url", c_short_url),
    Check("redirect", "Обёртка внешних ссылок redirect?to=", "normalize.unwrap_url / REDIRECT_RE", c_redirect),
    Check("keepalive", "Keep-alive: серия запросов без ошибок", "http.HttpClient / AdaptiveLimiter (темп, потоки)", c_keepalive),
]


def run_checks(user: str = "petra", on_result: Callable[[dict], Any] | None = None,
               only: list[str] | None = None, net: Any = None) -> list[dict]:
    x = _ctx(user, net)
    out = []
    try:
        for ch in CHECKS:
            if only and ch.key not in only:
                continue
            t = time.time()
            try:
                detail = ch.fn(x)
                r = {"key": ch.key, "name": ch.name, "ok": True, "detail": detail}
            except Warn as w:
                r = {"key": ch.key, "name": ch.name, "ok": False, "warn": True, "detail": str(w), "hint": ch.hint}
            except NoData as e:
                r = {"key": ch.key, "name": ch.name, "ok": False, "detail": f"нет данных от предыдущей проверки: {e}",
                     "hint": ch.hint}
            except Exception as e:  # noqa: BLE001
                r = {"key": ch.key, "name": ch.name, "ok": False, "detail": f"{type(e).__name__}: {e}"[:400],
                     "hint": ch.hint}
            r["seconds"] = round(time.time() - t, 2)
            out.append(r)
            if on_result:
                on_result(r)
    finally:
        x["client"].close()
    return out


def main(user: str = "petra") -> int:
    print(f"Проверка API DTF (профиль {user}) — подробности в docs/DTF_API.md\n")
    fails = 0

    def show(r: dict) -> None:
        nonlocal fails
        mark = "✓" if r["ok"] else ("⚠" if r.get("warn") else "✗")
        if not r["ok"] and not r.get("warn"):
            fails += 1
        print(f" {mark} {r['name']:<45} {r['detail']}")
        if not r["ok"]:
            print(f"     → смотреть: {r['hint']}")

    run_checks(user, show)
    print("\nИтог:", "всё в порядке" if not fails else f"проблем: {fails}")
    return 1 if fails else 0
