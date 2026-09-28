"""Shared normalization for renderers: safe HTML, Markdown and plain text from DTF content,
link unwrapping/rewriting, mentions and media resolution."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .api import MEDIA, SITE
from .media import media_key

REDIRECT_RE = re.compile(r"^https?://api\.dtf\.ru/v[\d.]+/redirect\?(.+)$", re.I)
POST_URL_RE = re.compile(r"^https?://(?:www\.|m\.)?dtf\.ru/(?:[^?#]*/)?(\d{3,})(?:-[^/?#]*)?/?(\?[^#]*)?(#.*)?$", re.I)
URL_RE = re.compile(r"(https?://[^\s<>\"'«»]+)", re.I)
MENTION_RE = re.compile(r'<mention\s+([^>]*)>(.*?)</mention>', re.S | re.I)
ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def unwrap_url(url: str) -> str:
    """DTF wraps external links in api.dtf.ru/v2.8/redirect?to=<url>."""
    if not url:
        return url
    m = REDIRECT_RE.match(url.strip())
    if m:
        to = urllib.parse.parse_qs(m.group(1)).get("to")
        if to:
            return to[0]
    return url.strip()


def safe_href(url: str) -> str | None:
    u = unwrap_url(html.unescape(url or ""))
    if not u:
        return None
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "#", "tg://")):
        return u
    if u.startswith("/"):
        return SITE + u
    return None


class Linker:
    """Rewrites dtf.ru post links to pages of the app when the post is in the archive.
    post_url(pid) builds the local URL; without it links stay absolute (Markdown/data exports)."""

    def __init__(self, local_posts: set[int], post_url: Callable[[int], str] | None = None):
        self.local_posts = local_posts
        self.post_url = post_url

    def local_post(self, pid: Any) -> str | None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return self.post_url(pid) if self.post_url and pid in self.local_posts else None

    def href(self, url: str) -> tuple[str, bool]:
        """(href, is_local)"""
        m = POST_URL_RE.match(url)
        segs = urllib.parse.urlsplit(url).path.strip("/").split("/")
        if m and not (segs and segs[0] in ("u", "s") and len(segs) == 2):  # /u/<id>-name is a profile
            local = self.local_post(m.group(1))
            if local:
                q = urllib.parse.parse_qs((m.group(2) or "?")[1:])
                frag = m.group(3) or ""
                if "comment" in q:
                    frag = f"#c{q['comment'][0]}"
                return local + frag, True
        return url, False


def profile_url(user_id: Any) -> str:
    return f"{SITE}/id{user_id}"


# ---------------------------------------------------------------- HTML conversion
ALLOWED = {"p", "br", "a", "b", "strong", "i", "em", "u", "s", "del", "strike", "code", "pre", "mark",
           "sup", "sub", "span", "ul", "ol", "li", "blockquote", "h2", "h3", "h4", "hr", "small"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template"}
VOID = {"br", "hr"}
MD_WRAP = {"b": "**", "strong": "**", "i": "*", "em": "*", "s": "~~", "del": "~~", "strike": "~~", "code": "`"}


class _Conv(HTMLParser):
    def __init__(self, linker: Linker | None):
        super().__init__(convert_charrefs=True)
        self.linker = linker
        self.h: list[str] = []
        self.md: list[str] = []
        self.txt: list[str] = []
        self.stack: list[str] = []
        self.skip = 0
        self.links: list[str] = []
        self.a_href: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "mention":
            uid = a.get("id")
            nick = a.get("nickname")
            href = profile_url(uid) if uid else (f"{SITE}/{nick}" if nick else "#")
            self.h.append(f'<a class="mention" href="{html.escape(href)}">')
            self.md.append("[")
            self.stack.append("mention")
            self.a_href.append(href)
            return
        if tag not in ALLOWED:
            return
        if tag == "a":
            href = safe_href(a.get("href", ""))
            local = False
            if href and self.linker:
                href, local = self.linker.href(href)
            if href:
                self.links.append(href)
                ext = "" if local or href.startswith("#") else ' target="_blank" rel="noopener"'
                self.h.append(f'<a href="{html.escape(href)}"{ext}>')
            else:
                self.h.append("<a>")
            self.a_href.append(href)
            self.md.append("[")
            self.stack.append("a")
            return
        if tag in VOID:
            self.h.append(f"<{tag}>")
            if tag == "br":
                self.md.append("  \n")
                self.txt.append("\n")
            else:
                self.md.append("\n\n---\n\n")
            return
        self.h.append(f"<{tag}>")
        self.stack.append(tag)
        if tag in MD_WRAP:
            self.md.append(MD_WRAP[tag])
        elif tag == "li":
            self.md.append("\n- ")
        elif tag in ("h2", "h3", "h4"):
            self.md.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "blockquote":
            self.md.append("\n\n> ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID:
            return
        if tag not in ALLOWED and tag != "mention":
            return
        if tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self._close(t)
            if t == tag:
                break

    def _close(self, t: str) -> None:
        if t in ("a", "mention"):
            self.h.append("</a>")
            href = self.a_href.pop() if self.a_href else None
            self.md.append(f"]({href})" if href else "]")
            return
        self.h.append(f"</{t}>")
        if t in MD_WRAP:
            self.md.append(MD_WRAP[t])
        elif t == "p":
            self.md.append("\n\n")
            self.txt.append("\n")
        elif t in ("li", "h2", "h3", "h4", "blockquote"):
            self.md.append("\n")
            self.txt.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.h.append(html.escape(data, quote=False))
        self.md.append(data)
        self.txt.append(data)

    def close_all(self) -> None:
        self.close()
        while self.stack:
            self._close(self.stack.pop())


class Rich:
    __slots__ = ("html", "md", "text", "links")

    def __init__(self, html_: str, md: str, text: str, links: list[str]):
        self.html, self.md, self.text, self.links = html_, md, text, links


def convert_html(src: str | None, linker: Linker | None = None) -> Rich:
    c = _Conv(linker)
    c.feed(src or "")
    c.close_all()
    md = re.sub(r"\n{3,}", "\n\n", "".join(c.md)).strip()
    text = re.sub(r"[ \t]+\n", "\n", "".join(c.txt))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Rich("".join(c.h), md, text, c.links)


def html_to_text(src: str | None) -> str:
    return convert_html(src).text


# ---------------------------------------------------------------- comment text
def _linkify(escaped: str, linker: Linker | None) -> str:
    def rep(m: re.Match) -> str:
        url = html.unescape(m.group(1))
        trail = ""
        while url and url[-1] in ".,;:!?)":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            trail = url[-1] + trail
            url = url[:-1]
        href = unwrap_url(url)
        local = False
        if linker:
            href, local = linker.href(href)
        ext = "" if local else ' target="_blank" rel="noopener"'
        return f'<a href="{html.escape(href)}"{ext}>{html.escape(url)}</a>{html.escape(trail)}'
    return URL_RE.sub(rep, escaped)


QUOTE_LINE_RE = re.compile(r"^\s*(?:>|&gt;)\s?")


def _inline(raw: str, linker: Linker | None) -> tuple[str, str, str]:
    """One line of comment text: plain text + <mention> tags -> (html, md, text)."""
    h: list[str] = []
    md: list[str] = []
    txt: list[str] = []
    pos = 0
    for m in MENTION_RE.finditer(raw):
        _plain(raw[pos:m.start()], h, md, txt, linker)
        attrs = dict(ATTR_RE.findall(m.group(1)))
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        href = profile_url(attrs["id"]) if attrs.get("id") else f"{SITE}/{attrs.get('nickname', '')}"
        h.append(f'<a class="mention" href="{html.escape(href)}" target="_blank" rel="noopener">@{html.escape(name.lstrip("@"))}</a>')
        md.append(f"[@{name.lstrip('@')}]({href})")
        txt.append("@" + name.lstrip("@"))
        pos = m.end()
    _plain(raw[pos:], h, md, txt, linker)
    return "".join(h), "".join(md), "".join(txt)


def comment_text(raw: str | None, linker: Linker | None = None) -> Rich:
    """DTF comment text is plain text (literal < and > allowed) plus <mention> tags.
    Consecutive lines starting with '>' are rendered as a quote."""
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    groups: list[tuple[bool, list[str]]] = []
    for ln in raw.split("\n"):
        q = bool(QUOTE_LINE_RE.match(ln))
        if groups and groups[-1][0] == q:
            groups[-1][1].append(ln)
        else:
            groups.append((q, [ln]))
    hs: list[str] = []
    mds: list[str] = []
    txts: list[str] = []
    for q, lines in groups:
        if q:
            parts = [_inline(QUOTE_LINE_RE.sub("", ln, count=1), linker) for ln in lines]
            hs.append('<blockquote class="cq">' + "<br>".join(p[0] for p in parts) + "</blockquote>")
            mds.append("\n".join("> " + p[1] for p in parts))
            txts.append("\n".join("> " + p[2] for p in parts))
        else:
            parts = [_inline(ln, linker) for ln in lines]
            if not any(p[2].strip() for p in parts):
                continue
            # blank lines separate paragraphs (small gap in CSS); single newlines are line breaks
            paras: list[list[str]] = [[]]
            for p in parts:
                if p[2].strip():
                    paras[-1].append(p[0])
                elif paras[-1]:
                    paras.append([])
            hs.append("".join("<p>" + "<br>".join(pp) + "</p>" for pp in paras if pp))
            mds.append("  \n".join(p[1] for p in parts).strip())
            txts.append("\n".join(p[2] for p in parts).strip())
    return Rich("".join(hs), "\n\n".join(m for m in mds if m).strip(), "\n".join(txts).strip(), [])



def _plain(s: str, h: list[str], md: list[str], txt: list[str], linker: Linker | None) -> None:
    if not s:
        return
    s = html.unescape(s)
    txt.append(s)
    md.append(s.replace("\r\n", "\n").replace("\n", "  \n"))
    esc = html.escape(s, quote=False).replace("\r\n", "\n")
    esc = _linkify(esc, linker)
    h.append(esc.replace("\n", "<br>"))


# ---------------------------------------------------------------- media
class MediaResolver:
    """uuid/url -> local file of the shared media store (see media.load_media_index)."""

    def __init__(self, index: dict[str, dict] | None = None):
        self.index: dict[str, dict] = index or {}
        self.used: dict[str, set[str]] = {}

    @classmethod
    def for_library(cls, library: Path) -> "MediaResolver":
        from .media import load_media_index
        return cls(load_media_index(library))

    def local(self, key: str | None) -> dict | None:
        if not key:
            return None
        e = self.index.get(key)
        if e and not e.get("missing"):
            return e
        return None

    def use(self, key: str, owner: str) -> None:
        self.used.setdefault(key, set()).add(owner)


def _int(v: Any) -> int | None:
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def media_info(obj: Any, resolver: MediaResolver, owner: str | None = None) -> dict | None:
    """Normalize a DTF media object ({"type": "image"|"movie"|..., "data": {...}}) or a bare data dict."""
    if not isinstance(obj, dict):
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    otype = obj.get("type") if "data" in obj else None
    key = media_key(data.get("uuid"))
    if not key:
        return None
    if owner:
        resolver.use(key, owner)
    loc = resolver.local(key)
    ftype = (data.get("type") or "").lower()
    mime = (loc or {}).get("mime") or ""
    animated = ftype in ("gif", "mp4", "webm", "mov") or bool(data.get("isVideo")) or otype in ("movie", "video")
    if mime:
        kind = "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else \
            "image" if mime.startswith("image/") else "file"
    else:
        kind = "video" if animated else "audio" if otype == "audio" else "file" if otype == "file" else "image"
    remote = key if key.startswith("http") else f"{MEDIA}/{key}/"
    if kind == "video" and not key.startswith("http"):
        remote = f"{MEDIA}/{key}/-/format/mp4/"
    return {
        "key": key, "kind": kind, "local": loc["path"] if loc else None, "remote": remote,
        "width": _int(data.get("width")), "height": _int(data.get("height")),
        "duration": data.get("duration"), "hasAudio": bool(data.get("has_audio")),
        "isGif": ftype == "gif" and not data.get("has_audio"),
        "size": (loc or {}).get("size") or _int(data.get("size")), "format": ftype or None,
        "name": data.get("name") or data.get("title") or None,
    }


def media_src(m: dict, root_rel: str) -> str:
    return root_rel + m["local"] if m.get("local") else m["remote"]


EXT_VIDEO = {
    "youtube": lambda i, t: f"https://www.youtube.com/watch?v={i}" + (f"&t={int(t)}s" if t else ""),
    "vimeo": lambda i, t: f"https://vimeo.com/{i}",
    "coub": lambda i, t: f"https://coub.com/view/{i}",
    "twitch": lambda i, t: f"https://www.twitch.tv/videos/{i}",
    "vk": lambda i, t: f"https://vk.com/video{i}",
    "rutube": lambda i, t: f"https://rutube.ru/video/{i}/",
}


def external_video_url(data: dict) -> tuple[str | None, str | None]:
    es = data.get("external_service")
    if isinstance(es, dict) and es.get("name"):
        name = str(es["name"]).lower()
        vid = str(es.get("id") or "")
        f = EXT_VIDEO.get(name)
        url = f(vid, data.get("time")) if f and vid else (data.get("url") or None)
        return name, url
    return None, None


def find_first(obj: Any, pred: Callable[[str, Any], bool], depth: int = 6) -> Any:
    if depth < 0:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if pred(k, v):
                return v
        for v in obj.values():
            r = find_first(v, pred, depth - 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_first(v, pred, depth - 1)
            if r is not None:
                return r
    return None


def slug_from_url(url: str | None) -> str:
    if not url:
        return ""
    last = urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]
    m = re.match(r"\d+-(.+)", last)
    s = m.group(1) if m else ""
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:60]
