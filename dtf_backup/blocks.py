"""Editor.js (Osnova) block rendering: HTML (site), Markdown (md/) and normalized JSON (data/).

Levels of support:
  * full     - dedicated renderer;
  * generic  - block type known from the DTF bundle but no samples: best-effort card
               (texts, links, media) + JSON spoiler;
  * unsupported / error - unknown type or renderer crashed: warning card + JSON spoiler.
Nothing is ever dropped: the original block JSON stays in raw/ and in data/posts.jsonl.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .api import SITE
from .media import collect_media
from .web.icons import icon
from .normalize import (Linker, MediaResolver, convert_html, external_video_url, media_info,
                        media_src, unwrap_url)

E = html.escape


class Report:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, dict]] = {"unsupported": {}, "generic": {}, "errors": {},
                                                    "unknownReactions": {}}

    def add(self, bucket: str, key: str, where: str, detail: str | None = None) -> None:
        b = self.buckets[bucket].setdefault(key, {"count": 0, "examples": []})
        b["count"] += 1
        ex = where + (f": {detail}" if detail else "")
        if len(b["examples"]) < 5 and ex not in b["examples"]:
            b["examples"].append(ex)


@dataclass
class Ctx:
    resolver: MediaResolver
    linker: Linker            # rewrites dtf.ru post links to app pages (or keeps them absolute)
    root_rel: str             # URL prefix for media paths ("/" in the app); media paths are "media/ab/<sha>.<ext>"
    md_root_rel: str          # md file -> library root (for Markdown media links)
    owner: str                # media usage owner, e.g. "post:123"
    where: str                # for the report, e.g. "post 123"
    report: Report
    local_posts: set[int] = field(default_factory=set)


# ------------------------------------------------------------------ media helpers
def media_html(m: dict | None, ctx: Ctx, cls: str = "", link: bool = True) -> str:
    if not m:
        return ""
    src = E(media_src(m, ctx.root_rel))
    remote = "" if m.get("local") else ' data-remote="1" title="Файл не сохранён локально — загружается с сервера DTF"'
    wh = dims = ""
    if m.get("width") and m.get("height"):
        wh = f' width="{int(m["width"])}" height="{int(m["height"])}"'
        dims = f' data-pswp-width="{int(m["width"])}" data-pswp-height="{int(m["height"])}"'
    if m["kind"] == "video":
        if m.get("hasAudio"):
            return f'<video class="media {cls}" controls preload="metadata" src="{src}"{wh}{remote}></video>'
        vid = (f'<video class="media gifv {cls}" muted loop playsinline preload="metadata" data-autoplay '
               f'src="{src}"{wh}{remote}></video>')
        # gif-like videos open in the PhotoSwipe lightbox too (as an html slide)
        return f'<a class="pswp-item" href="{src}" data-pswp-video{dims} target="_blank">{vid}</a>' if link else vid
    if m["kind"] == "audio":
        return f'<audio class="media {cls}" controls preload="none" src="{src}"{remote}></audio>'
    if m["kind"] == "file":
        name = E(m.get("name") or m["key"])
        return f'<a class="file {cls}" href="{src}" download{remote}>{icon("attach_file")}{name}</a>'
    img = f'<img class="media {cls}" loading="lazy" src="{src}"{wh} alt=""{remote}>'
    return f'<a class="pswp-item" href="{src}"{dims} target="_blank">{img}</a>' if link else img


def media_thumb(m: dict, ctx: Ctx) -> str:
    """Tile content for the gallery header: the file itself, fitted into a square (object-fit: contain)."""
    src = E(media_src(m, ctx.root_rel))
    if m["kind"] == "video":
        return f'<video muted playsinline preload="metadata" src="{src}"></video><span class="g-play">{icon("play_arrow")}</span>'
    if m["kind"] == "image":
        return f'<img loading="lazy" src="{src}" alt="">'
    return f'<span class="g-file">{E(m["kind"])}</span>'


def media_md(m: dict | None, ctx: Ctx, alt: str = "") -> str:
    if not m:
        return ""
    src = (ctx.md_root_rel + m["local"]) if m.get("local") else m["remote"]
    src = src.replace(" ", "%20")
    if m["kind"] == "image":
        return f"![{alt}]({src})"
    label = {"video": "🎬 видео", "audio": "🔊 аудио", "file": "📎 файл"}.get(m["kind"], "файл")
    return f"[{label}{': ' + alt if alt else ''}]({src})"


def media_norm(m: dict | None) -> dict | None:
    if not m:
        return None
    return {k: m[k] for k in ("key", "kind", "local", "remote", "width", "height", "duration", "hasAudio", "format", "size")
            if m.get(k) not in (None, False)}


def rich(text: str | None, ctx: Ctx) -> tuple[str, str, str]:
    """(html for the site, markdown, plain text)"""
    h = convert_html(text, ctx.linker).html
    r = convert_html(text, None)
    return h, r.md, r.text


def json_spoiler(obj: Any, label: str = "Показать JSON") -> str:
    return (f'<details class="json"><summary>{E(label)}</summary>'
            f'<pre>{E(json.dumps(obj, ensure_ascii=False, indent=2))}</pre></details>')


def all_media(obj: Any, ctx: Ctx) -> list[dict]:
    out = []
    for key, _sig, kind in collect_media(obj):
        m = media_info({"uuid": key, "type": kind}, ctx.resolver, ctx.owner)
        if m:
            out.append(m)
    return out


# ------------------------------------------------------------------ block renderers
Result = tuple[str, str, dict]


def b_text(d: dict, ctx: Ctx) -> Result:
    h, md, t = rich(d.get("text"), ctx)
    return f'<div class="b-text">{h}</div>', md, {"text": t, "html": convert_html(d.get("text")).html}


def b_header(d: dict, ctx: Ctx) -> Result:
    lvl = {"h1": 2, "h2": 2, "h3": 3, "h4": 4, "h5": 4, "h6": 4}.get(str(d.get("style", "h2")).lower(), 2)
    h, md, t = rich(d.get("text"), ctx)
    return f'<h{lvl} class="b-header">{h}</h{lvl}>', "#" * lvl + " " + md, {"level": lvl, "text": t}


def b_list(d: dict, ctx: Ctx) -> Result:
    items = d.get("items") or []
    ordered = str(d.get("type", "UL")).upper() == "OL"
    tag = "ol" if ordered else "ul"
    hs, mds, ts = [], [], []
    for i, it in enumerate(items, 1):
        h, md, t = rich(it if isinstance(it, str) else json.dumps(it, ensure_ascii=False), ctx)
        hs.append(f"<li>{h}</li>")
        mds.append((f"{i}. " if ordered else "- ") + md.replace("\n", " "))
        ts.append(t)
    return f'<{tag} class="b-list">{"".join(hs)}</{tag}>', "\n".join(mds), {"ordered": ordered, "items": ts}


def b_quote(d: dict, ctx: Ctx) -> Result:
    h, md, t = rich(d.get("text"), ctx)
    subs = [s for s in (d.get("subline1"), d.get("subline2")) if s]
    sub_h = ", ".join(rich(s, ctx)[0] for s in subs)
    sub_t = ", ".join(rich(s, ctx)[2] for s in subs)
    foot = f"<footer>— {sub_h}</footer>" if subs else ""
    md_q = "\n".join("> " + line for line in md.split("\n")) + (f"\n>\n> — {sub_t}" if subs else "")
    return (f'<blockquote class="b-quote">{h}{foot}</blockquote>', md_q,
            {"text": t, "author": sub_t or None, "style": d.get("type")})


def b_incut(d: dict, ctx: Ctx) -> Result:
    h, md, t = rich(d.get("text"), ctx)
    return (f'<aside class="b-incut">{h}</aside>', "\n".join("> " + x for x in md.split("\n")),
            {"text": t})


def b_delimiter(d: dict, ctx: Ctx) -> Result:
    return '<hr class="b-delim">', "* * *", {}


def b_media(d: dict, ctx: Ctx) -> Result:
    items = d.get("items") or []
    figs, mds, norm = [], [], []
    good: list[tuple[dict, str, str]] = []
    for it in items:
        m = media_info(it.get("image"), ctx.resolver, ctx.owner) if isinstance(it, dict) else None
        cap_h, cap_md, cap_t = rich(it.get("title"), ctx) if isinstance(it, dict) and it.get("title") else ("", "", "")
        if m is None:
            figs.append(f'<figure class="bad">{json_spoiler(it, "Элемент без медиа — JSON")}</figure>')
            ctx.report.add("errors", "media:item", ctx.where, "элемент без uuid")
            continue
        figs.append(f'<figure>{media_html(m, ctx)}{f"<figcaption>{cap_h}</figcaption>" if cap_t else ""}</figure>')
        good.append((m, cap_h, cap_t))
        mds.append(media_md(m, ctx, cap_t))
        norm.append({"media": media_norm(m), "caption": cap_t or None})
    title_h, _, title_t = rich(d.get("title"), ctx) if d.get("title") else ("", "", "")
    cap = f'<div class="gallery-title">{title_h}</div>' if title_t else ""
    md = "\n\n".join(mds) + (f"\n\n*{title_t}*" if title_t else "")
    norm_out = {"items": norm, "title": title_t or None}
    if len(good) <= 1 or len(figs) != len(good):
        return f'<div class="b-media single pswp-gallery">{"".join(figs)}{cap}</div>', md, norm_out
    # gallery: square tiles on top (click switches the main picture), the whole picture below
    n = len(good)
    tiles, slides = [], []
    for i, (m, cap_h, cap_t) in enumerate(good):
        on = " on" if i == 0 else ""
        tiles.append(f'<button type="button" class="g-tile{on}" data-i="{i}" title="{i + 1} из {n}" '
                     f'aria-label="Показать {i + 1} из {n}">{media_thumb(m, ctx)}</button>')
        slides.append(f'<figure class="g-slide{on}" data-i="{i}">{media_html(m, ctx)}'
                      f'{f"<figcaption>{cap_h}</figcaption>" if cap_t else ""}</figure>')
    return (f'<div class="b-media b-gallery pswp-gallery"><div class="g-tiles">{"".join(tiles)}'
            f'<span class="g-count muted small">{n} шт.</span></div>'
            f'<div class="g-stage">{"".join(slides)}</div>{cap}</div>', md, norm_out)


def _video_parts(v: dict, ctx: Ctx) -> tuple[str, str, dict]:
    vd = v.get("data") if isinstance(v.get("data"), dict) else v
    service, url = external_video_url(vd)
    thumb = media_info(vd.get("thumbnail"), ctx.resolver, ctx.owner) if vd.get("thumbnail") else None
    if service:
        th = media_html(thumb, ctx, link=False) if thumb else '<div class="noimg"></div>'
        label = {"youtube": "YouTube", "vimeo": "Vimeo", "coub": "Coub", "vk": "VK Видео", "twitch": "Twitch",
                 "rutube": "RuTube"}.get(service, service)
        href = E(url or "#")
        h = (f'<a class="ext-video" href="{href}" target="_blank" rel="noopener">{th}'
             f'<span class="play">{icon("play_arrow")}</span><span class="svc">{E(label)}</span></a>')
        md = f"[{media_md(thumb, ctx, label) if thumb else '▶ ' + label}]({url})" if url else f"▶ {label}"
        return h, md, {"service": service, "url": url, "id": (vd.get("external_service") or {}).get("id"),
                       "thumbnail": media_norm(thumb)}
    m = media_info(v, ctx.resolver, ctx.owner)
    if m:
        m["kind"] = "video" if m["kind"] == "image" and m.get("format") in ("mp4", "gif") else m["kind"]
        return media_html(m, ctx), media_md(m, ctx), {"media": media_norm(m), "thumbnail": media_norm(thumb)}
    raise ValueError("video без external_service и без uuid")


def b_video(d: dict, ctx: Ctx) -> Result:
    h, md, norm = _video_parts(d.get("video") or {}, ctx)
    title_h, _, title_t = rich(d.get("title"), ctx) if d.get("title") else ("", "", "")
    cap = f"<figcaption>{title_h}</figcaption>" if title_t else ""
    norm["title"] = title_t or None
    return f'<figure class="b-video">{h}{cap}</figure>', md + (f"\n\n*{title_t}*" if title_t else ""), norm


def _link_card(ld: dict, ctx: Ctx, cls: str = "b-link") -> tuple[str, str, dict]:
    url = unwrap_url(ld.get("url") or "")
    href, local = ctx.linker.href(url)
    img = media_info(ld.get("image"), ctx.resolver, ctx.owner) if ld.get("image") else None
    is_icon = bool(img and (img["key"].startswith("http") or (img.get("width") or 0) <= 64))
    title = ld.get("title") or ld.get("hostname") or url
    desc = ld.get("description") or ""
    host = ld.get("hostname") or (url.split("/")[2] if url.count("/") >= 2 else "")
    pic = ""
    if img:
        pic = f'<span class="lc-img{" icon" if is_icon else ""}">{media_html(img, ctx, link=False)}</span>'
    ext = "" if local else ' target="_blank" rel="noopener"'
    h = (f'<a class="{cls}" href="{E(href)}"{ext}>{pic}<span class="lc-body"><span class="lc-title">{E(title)}</span>'
         f'{f"<span class=lc-desc>{E(desc)}</span>" if desc else ""}<span class="lc-host">{E(host)}</span></span></a>')
    md = f"🔗 [{title}]({url})" + (f" — {desc}" if desc else "")
    return h, md, {"url": url, "title": ld.get("title"), "description": desc or None, "hostname": host or None,
                   "image": media_norm(img)}


def b_link(d: dict, ctx: Ctx) -> Result:
    lk = d.get("link") or {}
    ld = lk.get("data") if isinstance(lk.get("data"), dict) else lk
    if not ld.get("url"):
        raise ValueError("link без url")
    return _link_card(ld, ctx)


def b_osnova_embed(d: dict, ctx: Ctx) -> Result:
    oe = d.get("osnovaEmbed") or {}
    od = oe.get("data") if isinstance(oe.get("data"), dict) else oe
    pid = od.get("original_id")
    url = od.get("url") or (f"{SITE}/{pid}" if pid else "")
    href, local = ctx.linker.href(url) if url else ("#", False)
    if pid and ctx.linker.local_post(pid):
        href, local = ctx.linker.local_post(pid), True
    img = media_info(od.get("image"), ctx.resolver, ctx.owner) if od.get("image") else None
    author = (od.get("subsite") or {}).get("name") or (od.get("author") or {}).get("name") or ""
    na = ' <span class="muted">(недоступен)</span>' if od.get("isNotAvailable") else ""
    ext = "" if local else ' target="_blank" rel="noopener"'
    pic = f'<span class="lc-img">{media_html(img, ctx, link=False)}</span>' if img else ""
    h = (f'<a class="b-embed" href="{E(href)}"{ext}>{pic}<span class="lc-body">'
         f'<span class="lc-title">{E(od.get("title") or "Пост")}{na}</span>'
         f'<span class="lc-desc">{E(od.get("description") or "")}</span>'
         f'<span class="lc-host">{icon("inventory_2") + "в архиве · " if local else ""}{E(author)} · DTF</span></span></a>')
    md = f"📄 [{od.get('title') or 'Пост'}]({url})" + (f" — {od.get('description')}" if od.get("description") else "")
    return h, md, {"postId": pid, "url": url, "title": od.get("title"), "description": od.get("description"),
                   "author": author or None, "image": media_norm(img), "localPost": bool(local)}


def b_code(d: dict, ctx: Ctx) -> Result:
    text = d.get("text") or ""
    lang = d.get("lang") or ""
    return (f'<pre class="b-code" data-lang="{E(lang)}"><code>{E(text)}</code></pre>',
            f"```{lang}\n{text}\n```", {"lang": lang or None, "text": text})


def b_quiz(d: dict, ctx: Ctx) -> Result:
    items = d.get("items") or {}
    opts = list(items.values()) if isinstance(items, dict) else [str(x) for x in items]
    title = d.get("title") or ""
    lis = "".join(f"<li>{E(str(o))}</li>" for o in opts)
    h = (f'<div class="b-quiz"><div class="q-title">{icon("bar_chart")}{E(title)}</div><ul>{lis}</ul>'
         f'<div class="muted small">Опрос. Результаты голосования в архиве не сохраняются.</div></div>')
    md = f"**📊 Опрос: {title}**\n" + "\n".join(f"- [ ] {o}" for o in opts)
    return h, md, {"title": title, "options": opts, "hash": d.get("hash")}


def b_person(d: dict, ctx: Ctx) -> Result:
    img = media_info(d.get("image"), ctx.resolver, ctx.owner) if d.get("image") else None
    th, tmd, tt = rich(d.get("title"), ctx)
    dh, dmd, dt = rich(d.get("description"), ctx)
    h = (f'<div class="b-person pswp-gallery">{media_html(img, ctx) if img else ""}<div><div class="p-name">{th}</div>'
         f'<div class="p-desc">{dh}</div></div></div>')
    md = (media_md(img, ctx, tt) + "\n\n" if img else "") + f"**{tmd}** — {dmd}"
    return h, md, {"name": tt, "description": dt, "image": media_norm(img)}


def b_button(d: dict, ctx: Ctx) -> Result:
    url = d.get("url") or d.get("link") or d.get("href")
    if isinstance(url, dict):
        url = url.get("url")
    text = d.get("text") or d.get("title") or url
    if not url or not text:
        raise ValueError("кнопка без url/text")
    url = unwrap_url(str(url))
    href, local = ctx.linker.href(url)
    ext = "" if local else ' target="_blank" rel="noopener"'
    t = convert_html(str(text)).text
    return (f'<p class="b-button"><a class="btn" href="{E(href)}"{ext}>{E(t)}</a></p>', f"[{t}]({url})",
            {"text": t, "url": url})


def b_rawhtml(d: dict, ctx: Ctx) -> Result:
    raw = d.get("raw") or d.get("html") or d.get("text") or ""
    if not raw:
        raise ValueError("rawhtml без содержимого")
    h = (f'<div class="b-generic"><div class="g-head">HTML-вставка (показана как код, не исполняется)</div>'
         f'<pre class="b-code"><code>{E(raw)}</code></pre></div>')
    return h, f"```html\n{raw}\n```", {"html": raw}


def b_generic(t: str) -> Callable[[dict, Ctx], Result]:
    """Best-effort renderer for embed-like blocks without known samples."""
    def render(d: dict, ctx: Ctx) -> Result:
        texts: list[str] = []
        urls: list[str] = []

        def walk(o: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str):
                        if k in ("url", "link", "href", "src") and v.startswith("http"):
                            u = unwrap_url(v)
                            if "leonardo.osnova.io" not in u and u not in urls:
                                urls.append(u)
                        elif k in ("title", "text", "description", "name", "caption", "subline1", "subline2", "markdown"):
                            tt = convert_html(v).text.strip()
                            if tt and tt not in texts:
                                texts.append(tt[:600])
                    elif isinstance(v, (dict, list)):
                        walk(v, depth + 1)
            elif isinstance(o, list):
                for v in o:
                    walk(v, depth + 1)
        walk(d)
        media = all_media(d, ctx)
        body = "".join(f"<p>{E(x)}</p>" for x in texts[:4])
        links = "".join(f'<a href="{E(u)}" target="_blank" rel="noopener">{E(u)}</a><br>' for u in urls[:4])
        thumbs = "".join(media_html(m, ctx, cls="thumb") for m in media[:6])
        h = (f'<div class="b-generic" data-type="{E(t)}"><div class="g-head">Встраиваемый блок «{E(t)}»</div>'
             f'{body}{f"<div class=g-links>{links}</div>" if links else ""}'
             f'{f"<div class=\"g-media pswp-gallery\">{thumbs}</div>" if thumbs else ""}{json_spoiler(d)}</div>')
        md_parts = [f"> [{t}]"] + [f"> {x}" for x in texts[:4]] + [f"> {u}" for u in urls[:4]]
        md_parts += [media_md(m, ctx) for m in media[:6]]
        return h, "\n".join(md_parts), {"texts": texts, "urls": urls, "media": [media_norm(m) for m in media],
                                        "raw": d}
    return render


FULL: dict[str, Callable[[dict, Ctx], Result]] = {
    "text": b_text, "header": b_header, "list": b_list, "quote": b_quote, "incut": b_incut,
    "delimiter": b_delimiter, "media": b_media, "video": b_video, "link": b_link,
    "osnovaEmbed": b_osnova_embed, "code": b_code, "quiz": b_quiz, "person": b_person,
}
# Known from the DTF web bundle, but without real samples in the archive yet.
PARTIAL: dict[str, Callable[[dict, Ctx], Result]] = {
    "special_button": b_button, "telegram_button": b_button, "rawhtml": b_rawhtml,
}
GENERIC_TYPES = ("tweet", "telegram", "instagram", "tiktok", "yamusic", "spotify", "game", "audio", "number",
                 "embed", "movie")


def unsupported_html(t: str, block: Any, ctx: Ctx, error: str | None = None) -> str:
    media = all_media(block, ctx)
    thumbs = "".join(media_html(m, ctx, cls="thumb") for m in media[:8])
    why = (f'Не удалось отобразить блок <code>{E(t)}</code> ({E(error)})' if error
           else f'Неподдерживаемый блок: <code>{E(t)}</code>')
    return (f'<div class="b-unsupported" data-type="{E(t)}"><div class="u-head">{icon("warning")}{why}</div>'
            f'{f"<div class=\"g-media pswp-gallery\">{thumbs}</div>" if thumbs else ""}'
            f'{json_spoiler(block, "Показать исходные данные блока (JSON)")}</div>')


def unsupported_md(t: str, block: Any, error: str | None = None) -> str:
    why = f"не удалось отобразить ({error})" if error else "неподдерживаемый блок"
    return f"> ⚠ `{t}`: {why}\n\n```json\n{json.dumps(block, ensure_ascii=False, indent=2)}\n```"


def render_block(b: Any, ctx: Ctx) -> tuple[str, str, dict]:
    """-> (html, markdown, normalized). Never raises."""
    if not isinstance(b, dict):
        ctx.report.add("unsupported", f"<{type(b).__name__}>", ctx.where)
        return unsupported_html("?", b, ctx), unsupported_md("?", b), {"type": None, "supported": False, "raw": b}
    t = str(b.get("type") or "?")
    d = b.get("data") if isinstance(b.get("data"), dict) else {}
    level = "full"
    try:
        if t in FULL:
            h, md, norm = FULL[t](d, ctx)
        elif t in PARTIAL:
            try:
                h, md, norm = PARTIAL[t](d, ctx)
                level = "generic"
                ctx.report.add("generic", t, ctx.where)
            except Exception:
                h, md, norm = b_generic(t)(d, ctx)
                level = "generic"
                ctx.report.add("generic", t, ctx.where)
        elif t in GENERIC_TYPES:
            h, md, norm = b_generic(t)(d, ctx)
            level = "generic"
            ctx.report.add("generic", t, ctx.where)
        else:
            ctx.report.add("unsupported", t, ctx.where)
            h, md = unsupported_html(t, b, ctx), unsupported_md(t, b)
            norm = {"raw": b}
            level = "unsupported"
    except Exception as e:  # a known block with an unexpected shape
        err = f"{type(e).__name__}: {e}"
        ctx.report.add("errors", t, ctx.where, err[:200])
        h, md = unsupported_html(t, b, ctx, err), unsupported_md(t, b, err)
        norm = {"raw": b, "error": err}
        level = "error"
    out = {"type": t, "supported": {"full": True, "generic": "generic"}.get(level, False)}
    for k in ("anchor", "hidden", "cover"):
        if b.get(k):
            out[{"hidden": "spoiler"}.get(k, k)] = b[k]
    out.update(norm)
    anchor = f' id="{E(str(b["anchor"]))}"' if b.get("anchor") else ""
    if b.get("hidden"):
        h = f'<details class="spoiler"><summary>{icon("visibility")}Спойлер</summary>{h}</details>'
        md = "<details><summary>Спойлер</summary>\n\n" + md + "\n\n</details>"
    return f'<div class="blk blk-{E(t)}"{anchor}>{h}</div>', md, out


def render_blocks(blocks: list[Any], ctx: Ctx) -> tuple[str, str, list[dict]]:
    hs, mds, norms = [], [], []
    for b in blocks or []:
        h, md, n = render_block(b, ctx)
        hs.append(h)
        mds.append(md)
        norms.append(n)
    return "\n".join(hs), "\n\n".join(m for m in mds if m), norms
