"""Media discovery and content-addressed, deduplicating downloads.

Discovery walks the *whole* JSON (not only known fields), so files referenced from editor
blocks this tool does not understand yet are still archived.

Dedup levels:
  1. by key (uuid/url): each key is downloaded at most once (DTF uuids are mostly v5,
     i.e. deterministic, so identical re-uploads usually share a uuid);
  2. probe: if another downloaded blob has the same metadata signature, fetch only the first
     and last 64 KB via Range and compare with the local file; on match, no full download;
  3. sha256: storage is content-addressed (media/<sha[:2]>/<sha>.<ext>), so identical
     content is stored once even under different uuids.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from .api import media_url
from .http import HttpClient, HttpError

MIME_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/heic": "heic",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac",
    "application/pdf": "pdf", "application/zip": "zip",
}
PROBE = 1 << 16
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


AVATAR_SIZE = 72


def avatar_key(avatar: Any) -> str | None:
    """Small square avatar (~2 KB JPEG) for comment authors: {MEDIA}/<uuid>/-/scale_crop/72x72/."""
    if isinstance(avatar, dict):
        avatar = (avatar.get("data") or {}).get("uuid") if isinstance(avatar.get("data"), dict) else avatar.get("uuid")
    if not isinstance(avatar, str) or not UUID_RE.match(avatar):
        return None
    return f"https://leonardo.osnova.io/{avatar.lower()}/-/scale_crop/{AVATAR_SIZE}x{AVATAR_SIZE}/"


def raw_key(uuid: Any) -> str | None:
    """Original file without CDN conversion (animated WebP for reactions/badges): <uuid>/-/format/raw/."""
    if not isinstance(uuid, str) or not UUID_RE.match(uuid):
        return None
    return f"https://leonardo.osnova.io/{uuid.lower()}/-/format/raw/"


def signature(d: dict) -> str | None:
    size = d.get("size")
    if not isinstance(size, int) or size <= 0:
        return None
    return "|".join(str(d.get(k, "")) for k in ("size", "width", "height", "type", "color", "duration"))


def media_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if UUID_RE.match(v):
        return v.lower()
    if v.startswith("https://leonardo.osnova.io/"):
        return v
    return None


def collect_media(obj: Any, skip_keys: Iterable[str] = ()) -> list[tuple[str, str | None, str | None]]:
    """Every media reference found anywhere in obj: (key, signature, kind)."""
    skip = set(skip_keys)
    out: dict[str, tuple[str, str | None, str | None]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            key = media_key(o.get("uuid"))
            if key and key not in out:
                out[key] = (key, signature(o), o.get("type") if isinstance(o.get("type"), str) else None)
            for k, v in o.items():
                if k in skip or k == "base64preview":
                    continue
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return list(out.values())


def _ext(mime: str | None, kind: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m in MIME_EXT:
        return MIME_EXT[m]
    if kind and re.fullmatch(r"[a-z0-9]{2,5}", kind):
        return kind
    return "bin"


def _read_head_tail(path: Path, n: int) -> tuple[bytes, bytes]:
    with open(path, "rb") as f:
        head = f.read(n)
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        tail = f.read(n)
    return head, tail


def _total_from_range(headers: dict[str, str]) -> int | None:
    cr = headers.get("content-range", "")
    m = re.search(r"/(\d+)\s*$", cr)
    return int(m.group(1)) if m else None


# Files being downloaded by this process: syncs of different archives often need the same file (avatars,
# reactions, reposted pictures); the second one waits for the first instead of downloading it again.
_INFLIGHT: dict[str, threading.Event] = {}
_DONE: dict[str, dict] = {}
_INFLIGHT_LOCK = threading.Lock()


class Downloader:
    def __init__(self, client: HttpClient, media_root: Path, tmp: Path | None = None):
        self.client = client
        self.root = media_root
        self.tmp = tmp or media_root / ".tmp"

    def blob_path(self, sha: str, ext: str) -> Path:
        return self.root / sha[:2] / f"{sha}.{ext}"

    def probe(self, key: str, candidates: list[dict]) -> dict | None:
        """Try to prove that `key` equals an already stored blob without downloading it."""
        url = media_url(key)
        r = self.client.fetch_range(url, f"bytes=0-{PROBE - 1}")
        if r.status != 206:
            return None
        total = _total_from_range(r.headers)
        if total is None:
            return None
        tail_body: bytes | None = None
        for c in candidates:
            if c["size"] != total:
                continue
            p = self.root / c["path"]
            if not p.exists():
                continue
            head, tail = _read_head_tail(p, PROBE)
            if head != r.body:
                continue
            if total > PROBE:
                if tail_body is None:
                    rt = self.client.fetch_range(url, f"bytes=-{PROBE}")
                    if rt.status != 206:
                        return None
                    tail_body = rt.body
                if tail != tail_body:
                    continue
            return {"status": "done", "sha256": c["sha256"], "via": "probe"}
        return None

    def fetch(self, key: str, kind: str | None, candidates: list[dict]) -> dict:
        """Runs in a worker thread. Returns a result dict for the main thread to commit."""
        with _INFLIGHT_LOCK:
            ev = _INFLIGHT.get(key)
            owner = ev is None
            if owner:
                ev = _INFLIGHT[key] = threading.Event()
        if not owner:
            ev.wait(900)
            with _INFLIGHT_LOCK:
                other = _DONE.get(key)
            if other and other.get("status") == "done":
                return dict(other, via="dedup")
        res = self._fetch(key, kind, candidates)
        if owner:
            with _INFLIGHT_LOCK:
                _DONE[key] = res
                if len(_DONE) > 5000:
                    for k in list(_DONE)[:1000]:
                        _DONE.pop(k, None)
                _INFLIGHT.pop(key, None)
            ev.set()
        return res

    def _fetch(self, key: str, kind: str | None, candidates: list[dict]) -> dict:
        try:
            if candidates:
                res = self.probe(key, candidates)
                if res:
                    return res
            part = self.tmp / (hashlib.sha1(key.encode()).hexdigest() + ".part")
            r = self.client.download(media_url(key), part)
            ext = _ext(r.headers.get("content-type"), kind)
            sha = r.sha256 or ""
            final = self.blob_path(sha, ext)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists() and final.stat().st_size == r.size:
                part.unlink(missing_ok=True)
                via = "dedup"
            else:
                os.replace(part, final)
                via = "download"
            return {"status": "done", "sha256": sha, "size": r.size, "ext": ext, "via": via,
                    "mime": (r.headers.get("content-type") or "").split(";")[0],
                    "path": final.relative_to(self.root).as_posix()}
        except HttpError as e:
            if e.status in (403, 404, 410, 451):
                return {"status": "missing", "error": f"HTTP {e.status}"}
            return {"status": "error", "error": str(e)[:300]}
        except OSError as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}


def load_media_index(library: Path) -> dict[str, dict]:
    """Shared catalog as key -> blob info ("path" is relative to the library root, e.g. media/ab/<sha>.jpg)."""
    from .state import open_store, store_path
    idx: dict[str, dict] = {}
    if not store_path(library).exists():
        return idx
    db = open_store(library)
    for row in db.execute(
            "SELECT r.key, r.status, b.sha256, b.path, b.size, b.mime FROM store.media_ref r "
            "LEFT JOIN store.blob b ON b.sha256 = r.sha256 WHERE r.status IN ('done','missing')"):
        if row["status"] == "done" and row["path"]:
            idx[row["key"]] = {"sha256": row["sha256"], "path": "media/" + row["path"],
                               "size": row["size"], "mime": row["mime"]}
        elif row["status"] == "missing":
            idx[row["key"]] = {"missing": True}
    db.close()
    return idx
