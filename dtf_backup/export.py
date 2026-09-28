"""Agent-friendly exports of one archive: data/*.jsonl, md/, README.md (no HTML; the app renders pages)."""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import __version__
from .api import SITE, comment_url
from .blocks import Ctx, Report, media_norm, render_blocks
from .context import ancestors, descendants
from .normalize import Linker, MediaResolver, comment_text, media_info, slug_from_url
from .reactions import Reactions, reaction_pairs, reactions_total
from .util import MSK, atomic_write_text, dumps, human_bytes, ts_human, ts_iso, write_json
from .viewdb import Dataset, group_view, post_cover, post_lead
from .web.ui import CommentView, Links, md_tree, month_title

MD_ROOT = "../../../"   # md/<kind>/<file>.md -> library root (archive/), where media/ lives


def _date(ts: int | None) -> str:
    return _dt.datetime.fromtimestamp(ts or 0, MSK).strftime("%Y-%m-%d")


def export(ds: Dataset, groups: "OrderedDict[str, list[dict]]", resolver: MediaResolver, rx: Reactions,
           report: Report, progress: Any = None) -> dict:
    arch = ds.arch
    step = progress or (lambda *_: None)
    tmp = {name: arch.root / f".{name}.tmp" for name in ("md", "data")}
    for p in tmp.values():
        shutil.rmtree(p, ignore_errors=True)
        p.mkdir(parents=True)
    mdd, data = tmp["md"], tmp["data"]
    links = Links(ds.nick)
    cv = CommentView(resolver, ds.local_posts, ds.users, ds.uid, report, None, rx, MD_ROOT)
    linker = Linker(ds.local_posts, None)

    # ------------------------------------------------------------ posts
    n_pc = 0
    with open(data / "posts.jsonl", "w", encoding="utf-8") as fp, \
            open(data / "post-comments.jsonl", "w", encoding="utf-8") as fpc:
        for i, p in enumerate(ds.posts):
            pid = p["id"]
            ctx = Ctx(resolver, linker, "/", MD_ROOT, f"post:{pid}", f"post {pid}", report, ds.local_posts)
            _, blocks_md, blocks_norm = render_blocks(p.get("blocks") or [], ctx)
            repost_md, repost_norm = _repost(p, ctx)
            title = p.get("title") or ""
            url = p.get("url") or f"{SITE}/{pid}"
            counters = p.get("counters") or {}
            pairs = reaction_pairs(p)
            items = ds.trees.get(pid, [])
            idx = ds.entry_index(pid) or ({}, {})
            roots = [c["id"] for c in items if not c["replyTo"] or c["replyTo"] not in idx[0]]
            fm = {"id": pid, "title": title, "date": ts_iso(p.get("date")), "dateModified": ts_iso(p.get("dateModified")),
                  "url": url, "comments": counters.get("comments", 0), "reactions": reactions_total(p),
                  "reactionsPositive": rx.split(pairs)[0], "reactionsNegative": rx.split(pairs)[1],
                  "favorites": counters.get("favorites", 0), "repost": bool(p.get("repostId"))}
            md = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()] + ["---", "",
                                                                                                  f"# {title or 'Без заголовка'}", ""]
            if repost_md:
                md += [repost_md, ""]
            md += [blocks_md, ""]
            if items:
                md += ["", f"## Комментарии ({len(items)})", ""] + md_tree(roots, idx[0], idx[1], cv)
            slug = slug_from_url(url)
            md_name = f"{_date(p.get('date'))}-{pid}" + (f"-{slug}" if slug else "") + ".md"
            atomic_write_text(mdd / "posts" / md_name, "\n".join(md) + "\n")
            cover = post_cover(p, resolver)
            fp.write(dumps({"id": pid, "url": url, "title": title, "date": p.get("date"), "dateIso": ts_iso(p.get("date")),
                            "dateModified": p.get("dateModified"), "subsite": _subsite(p),
                            "isRepost": bool(p.get("repostId")), "repostOf": repost_norm, "counters": counters,
                            "reactions": rx.norm(pairs), "lead": post_lead(p), "cover": media_norm(cover),
                            "blocks": blocks_norm, "unlisted": ds.unlisted(pid), "source": p.get("_source", "content"),
                            "siteState": (p.get("_site") or {}).get("state"),
                            "local": {"app": links.post(pid), "md": f"md/posts/{md_name}"}}) + "\n")
            for c in items:
                fpc.write(dumps({"id": c["id"], "postId": pid, "parentId": c["replyTo"] or None, "level": c["level"],
                                 "date": c["date"], "author": c["author"], "isMine": c["author"] == ds.uid,
                                 "text": comment_text(c["text"]).text, "media": cv.media_parts(c)[2] if c["media"] else [],
                                 "likes": c["likes"], "reactions": rx.norm(c["rx"]), "isRemoved": c["isRemoved"],
                                 "siteState": c.get("site"), "url": comment_url(pid, c["id"])}) + "\n")
                n_pc += 1
            step("export-posts", i + 1, len(ds.posts))

    # ------------------------------------------------------------ my comments (jsonl + grouped md)
    ctx_stats: dict[str, int] = {}
    with open(data / "comments.jsonl", "w", encoding="utf-8") as fp:
        for c in ds.my:
            row, status = _comment_row(ds, c, rx, cv, links)
            ctx_stats[status] = ctx_stats.get(status, 0) + 1
            fp.write(dumps(row) + "\n")
    for i, (ym, lst) in enumerate(groups.items()):
        out = [f"# Комментарии: {ds.prof.get('name') or ds.nick}, {month_title(ym)} ({sum(len(g['mine']) for g in lst)})", ""]
        for g in lst:
            out.extend(_md_group(ds, g, cv))
        atomic_write_text(mdd / "comments" / f"{ym}.md", "\n".join(out) + "\n")
        step("export-months", i + 1, len(groups))

    # ------------------------------------------------------------ context, users, catalogs
    my_ids = {c["id"] for c in ds.my}
    n_ctx = 0
    with open(data / "context.jsonl", "w", encoding="utf-8") as fp:
        for eid, items in ds.threads.items():
            for s in items:
                if s["id"] in my_ids:   # already in comments.jsonl; my comments missing from the feed
                    continue            # (e.g. removed by moderators) stay here with isMine=true
                mnorm = []
                for m in s["media"]:
                    mi = media_info(m, resolver) if isinstance(m, dict) and m.get("type") in ("image", "movie") else None
                    mnorm.append({"type": m.get("type"), **(media_norm(mi) or {})} if mi else {"type": m.get("type"), "raw": m})
                fp.write(dumps({"id": s["id"], "postId": eid, "parentId": s["replyTo"] or None, "level": s["level"],
                                "date": s["date"], "author": s["author"], "isMine": s["author"] == ds.uid,
                                "text": comment_text(s["text"]).text, "media": mnorm, "likes": s["likes"],
                                "reactions": rx.norm(s["rx"]), "isRemoved": s["isRemoved"], "siteState": s.get("site")}) + "\n")
                n_ctx += 1
    write_json(data / "users.json", {str(k): v for k, v in ds.users.items()}, pretty=False)
    write_json(data / "profile.json", ds.prof)
    write_json(data / "reactions.json", rx.catalog())
    n_media = _media_catalog(ds, data, resolver)

    for name, p in tmp.items():
        _swap(p, arch.root / name)
    counts = {"posts": len(ds.posts), "myComments": len(ds.my), "postComments": n_pc, "contextComments": n_ctx,
              "mediaRefs": n_media}
    _readme(ds, counts)
    return {"context": ctx_stats, "counts": counts}


# ---------------------------------------------------------------------- helpers
def _subsite(p: dict) -> dict | None:
    s = p.get("subsite") or {}
    return {"id": s.get("id"), "name": s.get("name"), "uri": s.get("uri")} if s else None


def _repost(p: dict, ctx: Ctx) -> tuple[str, dict | None]:
    rd = (p.get("repostData") or {}).get("data")
    if not isinstance(rd, dict):
        return "", None
    oid = rd.get("original_id")
    url = rd.get("url") or (f"{SITE}/{oid}" if oid else "")
    author = (rd.get("author") or {}).get("name") or (rd.get("subsite") or {}).get("name") or ""
    _, bmd, bnorm = render_blocks(rd.get("blocks") or [], ctx)
    md = f"> 🔁 Репост: [{rd.get('title') or 'Пост'}]({url}) — {author}\n\n{bmd}"
    return md, {"postId": oid, "url": url, "title": rd.get("title"), "author": author, "date": rd.get("date"),
                "blocks": bnorm}


def _comment_row(ds: Dataset, c: dict, rx: Reactions, cv: CommentView, links: Links) -> tuple[dict, str]:
    entry = c.get("entry") or {}
    eid = entry.get("id")
    own = bool(eid and eid in ds.local_posts)
    idx = ds.entry_index(eid)
    anc: list[int] = []
    reps: list[int] = []
    needed = c["level"] > 0 or c["replyCount"] > 0
    if idx and c["id"] in idx[0]:
        anc = [a for a in ancestors(c["id"], idx[0]) if a in idx[0]]
        reps = descendants(c["id"], idx[1])
        status = "ok"
    elif not needed:
        status = "not_needed"
    elif idx is None:
        status = "not_fetched"
    else:
        status = "missing"
    if not eid:
        status = "no_post"
    row = {"id": c["id"], "date": c["date"], "dateIso": ts_iso(c["date"]),
           "url": comment_url(eid, c["id"]) if eid else None,
           "post": {"id": eid, "title": entry.get("title"), "subsiteId": entry.get("subsiteId"),
                    "subsiteName": entry.get("subsiteName"), "isOwn": own},
           "text": comment_text(c["text"]).text, "media": cv.media_parts(c)[2] if c["media"] else [],
           "parentId": c["replyTo"] or None, "level": c["level"], "replyCount": c["replyCount"], "likes": c["likes"],
           "reactions": rx.norm(c["rx"]), "isEdited": c["isEdited"], "isRemoved": c["isRemoved"], "siteState": c.get("site"),
           "ancestorIds": anc, "replyIds": reps, "contextStatus": status,
           "local": {"app": links.go(c["id"]), "md": f"md/comments/{c['date'] and _date(c['date'])[:7]}.md"}}
    return row, status


def _md_group(ds: Dataset, g: dict, cv: CommentView) -> list[str]:
    eid = g["entry_id"]
    title = ds.entry_title(eid) or "Пост"
    url = f"{SITE}/{eid}" if eid else ""
    out = [f"## {ts_human(g['last_date'])} — [{title}]({url})", ""]
    gv = group_view(ds.entry_index(g["entry_id"]), g["mine"], g["root_id"])
    if gv["standalone"]:
        my = ds.my_by_id
        for cid in g["mine"]:
            if cid in my:
                out.append("- " + cv.md_line(my[cid]))
        return out + [""]
    for i, a in enumerate(gv["chain"]):
        if i:
            out.append(">")
        out.append("> " + cv.md_line(gv["by_id"][a]))
    if gv["chain"]:
        out.append("")
    out.extend(md_tree([gv["cur"]], {k: gv["by_id"][k] for k in gv["keep"]}, gv["kids"], cv))
    return out + [""]


def _media_catalog(ds: Dataset, data: Path, resolver: MediaResolver) -> int:
    usage: dict[str, list[str]] = {}
    for key, owner in ds.arch.db.execute("SELECT key, owner FROM main.media_use"):
        usage.setdefault(key, []).append(owner)
    with open(data / "media.jsonl", "w", encoding="utf-8") as f:
        for k in sorted(usage):
            e = resolver.index.get(k) or {}
            row = {"key": k, "sha256": e.get("sha256"), "path": e.get("path"), "size": e.get("size"),
                   "mime": e.get("mime"), "missing": bool(e.get("missing")) or None, "usedBy": sorted(usage[k])[:100]}
            f.write(dumps({kk: v for kk, v in row.items() if v not in (None, [])}) + "\n")
    return len(usage)


def _swap(tmp: Path, final: Path) -> None:
    import os
    old = final.with_name(final.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    try:
        if final.exists():
            os.replace(final, old)
        os.replace(tmp, final)
        shutil.rmtree(old, ignore_errors=True)
    except OSError:
        shutil.copytree(tmp, final, dirs_exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _readme(ds: Dataset, counts: dict) -> None:
    prof = ds.prof
    name = prof.get("name")
    uri = (prof.get("uri") or "").strip("/")
    txt = f"""# Архив DTF: {name} (@{uri}, id {prof.get('id')})

Копия постов и комментариев пользователя [{name}]({prof.get('url')}) на DTF.
Создано приложением LDTF (Local DTF) {__version__}. Данные собраны: {_dt.datetime.now(MSK):%d.%m.%Y %H:%M} (МСК).

**Смотреть:** запустите `LDTF.cmd` в папке приложения (двумя уровнями выше): LDTF откроется в браузере, значок — в трее.
**Без приложения:** Markdown в `md/` (читается любым редактором) и данные в `data/`.

| Что | Сколько |
|---|---|
| Посты пользователя | {counts['posts']} |
| Комментарии пользователя (со всего сайта) | {counts['myComments']} |
| Комментарии под постами пользователя (все авторы) | {counts['postComments']} |
| Комментарии других людей из веток обсуждений | {counts['contextComments']} |
| Медиа этого архива (ссылок) | {counts['mediaRefs']} |

## Структура

```
archive/                       библиотека приложения
  media/<sha[:2]>/<sha256>.<ext>   ОБЩЕЕ хранилище медиа всех архивов (по содержимому, без дублей)
  .state/media.sqlite              общий каталог медиа: media_ref(key → sha256), blob(sha256, path, size, mime)
  {ds.nick}/                         этот архив
    md/posts/                        посты в Markdown (front matter + текст + дерево комментариев)
    md/comments/ГГГГ-ММ.md           комментарии пользователя за месяц, сгруппированные по веткам обсуждений
    data/                            нормализованные данные (JSON Lines, UTF-8) для скриптов и ИИ-агентов
    raw/                             сырые ответы API DTF (.json.gz) — источник истины без потерь
    .state/state.sqlite              прогресс синхронизации; media_use — какие медиа нужны архиву
    .state/view.sqlite               база для приложения (посты, комментарии, группы, FTS5-поиск)
    reactions.config.json            какие реакции считать дизлайками
```

## Данные для агентов (`data/`)

Даты — unix time (секунды) плюс ISO-строка в МСК. Тексты — чистый текст без HTML.
Пути медиа (`local`, `path`) указаны относительно папки `archive/`, например `media/ab/<sha>.jpg`.

- `profile.json` — сырой профиль.
- `posts.jsonl` — 1 строка = 1 пост: `id, url, title, date, dateIso, dateModified, subsite, isRepost, repostOf,
  counters, reactions, lead, cover, blocks[], unlisted, local{{app, md}}`. Блок: `{{type, supported, anchor?, spoiler?,
  cover?, ...поля}}`, `supported`: `true` — полная поддержка, `"generic"` — упрощённо (исходник в `raw`), `false` —
  неизвестный тип (исходный JSON в `raw`). Медиа: `{{key, kind, local, remote, width, height, ...}}`.
- `post-comments.jsonl` — все комментарии под постами пользователя: `id, postId, parentId, level, date, author, isMine,
  text, media, likes, reactions, isRemoved, url`.
- `comments.jsonl` — 1 строка = 1 комментарий пользователя: `id, date, url, post{{id, title, subsiteId, subsiteName, isOwn}},
  text, media, parentId, level, replyCount, likes, reactions, isEdited, isRemoved, ancestorIds[] (от корня к
  родителю), replyIds[] (всё поддерево ответов), contextStatus (ok|not_needed|missing|not_fetched|no_post), local`.
  Тексты из `ancestorIds`/`replyIds` лежат в `context.jsonl` (чужие посты) или `post-comments.jsonl` (посты пользователя).
- `context.jsonl` — комментарии из веток вокруг комментариев пользователя: `id, postId, parentId, level, date, author, isMine, text,
  media, likes, reactions`. Комментарии пользователя, которых нет в ленте профиля (например, удалённые модератором), тоже
  здесь, с `isMine: true`.
- `users.json` — авторы: `id -> {{name, nickname, uri, avatar}}`.
- `reactions.json` — каталог реакций: `id, label, type, polarity (positive|negative), retired, static/animated`.
  Поле `reactions`: `{{total, positive, negative, items[{{id, count, polarity, label?, unknown?}}]}}`. DTF считает любую
  реакцию как +1 (`likes`); деление на ▲/▼ задаётся в `reactions.config.json`.
- `media.jsonl` — медиа архива: `key (uuid|url), sha256, path, size, mime, missing?, usedBy[]`
  (`post:<id>`, `mc:<id>` — комментарий пользователя, `pc:<id>` — комментарий под постом пользователя, `avatar:<id>`, `reaction:<id>`, `profile`).

SQL: `.state/view.sqlite` (схема в докстринге `dtf_backup/viewdb.py`) — те же данные плюс поисковый индекс: тексты в `search_docs`, FTS5 `fts` (исходные слова и основы), словарь `vocab`. Готовый поиск с морфологией и исправлением опечаток: `dtf_backup.search.engine.search(db, "запрос")`.

## Обновление

В приложении: кнопка «Синхронизировать» или автосинхронизация при запуске (настройки архива).
Из консоли (для скриптов): `python -m dtf_backup sync --user {uri}`; `render` пересобирает `view.sqlite`, `md/`
и `data/` без сети; `status` показывает прогресс; `check-api` проверяет, что API DTF отвечает как ожидается.
Как устроены данные DTF и API: `docs/DTF_API.md` в папке приложения.
"""
    atomic_write_text(ds.arch.root / "README.md", txt)
