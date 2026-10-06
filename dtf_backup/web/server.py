"""HTTP server of LDTF (stdlib ThreadingHTTPServer): 127.0.0.1 by default, any address with `serve --host`.

Security on this computer (127.0.0.1): requests must carry a local Host header (no DNS rebinding); every form POST
needs the CSRF token of the library (kept in archive/.state/app.secret, so a page opened before a restart still works)
and, when present, a local Origin (other sites in the browser can't trigger actions). The JSON job API used by the CLI
(`dtf-backup sync` while the app runs) needs the per-run token from archive/.state/app.run.json.
Beyond this computer (`--host 0.0.0.0`, Docker): any Host, a POST must come from the page's own origin, and every
page needs the password (web/auth.py) unless LDTF_AUTH=off says a reverse proxy checks who comes in.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import http.cookies
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import DEFAULT_PORT, __version__, config
from ..appsettings import load_app_settings, save_app_settings
from ..media import avatar_key, lookup
from ..netpool import NetPool
from ..normalize import MediaResolver
from ..reactions import config_path
from ..scheduler import Scheduler, next_sync
from ..scope import comments_kept, pending_drop
from ..state import META_GUARD, Archive, archive_dirs, gc_pending, peek_meta
from ..util import atomic_write_text, log, read_json_gz, write_json
from ..viewdb import open_view, view_meta, view_outdated, view_ready
from . import app_pages, insights, viewer
from .jobs import KINDS, RUNNING, JobManager
from .ui import ASSETS, Links, Shell, avatar_src, btn, empty_state

E = html.escape

MIME = {".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm", ".avif": "image/avif",
        ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".json": "application/json", ".ico": "image/x-icon", ".m4a": "audio/mp4", ".mp3": "audio/mpeg"}
NICK_RE = re.compile(r"^[\w.\-]{1,64}$")
FLASH = "<!--flash-->"   # where a page shows the outcome of the form that led to it (App.flash)


def library_id(library: Path) -> str:
    """Which library a running LDTF serves (/api/ping): another copy of LDTF on the default port is not this one."""
    return hashlib.sha256(os.path.normcase(str(library.resolve())).encode("utf-8")).hexdigest()[:16]


class App:
    def __init__(self, library: Path, port: int, host: str = "127.0.0.1"):
        self.library = library.resolve()
        self.library.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.host = host
        self.remote = not config.is_loopback(host)   # reachable beyond this computer: Host is not checked
        self.library_id = library_id(self.library)
        self.csrf = form_secret(self.library)
        self.shell = Shell(self.csrf)
        self.lock = threading.Lock()
        self._views: dict[str, tuple[tuple, viewer.ArchiveView]] = {}
        self._pages: OrderedDict[tuple, str] = OrderedDict()
        self._accounts: tuple[float, list[dict]] | None = None
        self.diag: dict[str, Any] = {"state": "idle", "results": [], "started": None, "finished": None}
        self._settings = load_app_settings(self.library)
        self.net = NetPool()   # one network budget for every sync and lookup
        self.jobs = JobManager(self.library, on_change=self.invalidate, net=self.net, on_finish=self._job_finished)
        self.jobs.auto_retry = lambda nick: self.next_run(nick) is not None
        self.scheduling = True   # False: started without automatic syncs (serve --no-auto-sync)
        self._flash: dict[str, tuple[float, str]] = {}   # path -> (time, HTML): the outcome of a form, shown once
        self.req = threading.local()  # per-request: archive from the path (nick) and the last opened one (cookie)
        self.token = secrets.token_urlsafe(24)  # JSON job API (CLI), stored in the run file
        self.scheduler = Scheduler(self)
        self.finish_listeners: list[Any] = []   # the tray shows notifications
        self.on_quit: Any = None                 # set by the runner (tray / console)
        self._rebuild_timer: threading.Timer | None = None

    # ------------------------------------------------------------------ app settings, status
    def settings(self) -> dict:
        return self._settings

    def save_settings(self, values: dict) -> dict:
        """Change the given keys (the rest stay)."""
        s = self._settings = save_app_settings(self.library, values)
        return s

    def _job_finished(self, job: Any) -> None:
        for cb in list(self.finish_listeners):
            try:
                cb(job)
            except Exception:  # noqa: BLE001
                log.exception("[app] уведомление о задании")

    def next_run(self, nick: str) -> float | None:
        """When the scheduler will sync this archive (None: schedule off / paused / stopped by the archive guard)."""
        if not (self.scheduling and self._settings["autosync"]) or not NICK_RE.match(nick):
            return None
        return next_sync(self.library / nick)

    # ------------------------------------------------------------------ outcome of a form (post / redirect / get)
    def flash(self, path: str, html_: str) -> None:
        with self.lock:
            self._flash[path] = (time.time(), html_)

    def take_flash(self, path: str) -> str:
        with self.lock:
            t, h = self._flash.pop(path, (0.0, ""))
            for k in [k for k, (ts, _) in self._flash.items() if time.time() - ts > 120]:
                del self._flash[k]
        return h if time.time() - t < 120 else ""

    def request_quit(self) -> None:
        if self.on_quit:
            threading.Timer(0.3, self.on_quit).start()

    # ------------------------------------------------------------------ archives
    def archive(self, nick: str) -> Archive | None:
        if not NICK_RE.match(nick):
            return None
        d = self.library / nick
        return Archive(d, self.library) if d.is_dir() else None

    def accounts(self) -> list[dict]:
        with self.lock:
            if self._accounts and time.time() - self._accounts[0] < 3:
                cached = self._accounts[1]
            else:
                cached = None
        if cached is None:
            out, profiles = [], []
            for d in archive_dirs(self.library):
                info: dict[str, Any] = {"nick": d.name, "name": d.name, "avatar": None, "comments": 0, "posts": 0,
                                        "built": False, "guard": peek_meta(d, META_GUARD)}
                arch = Archive(d, self.library)
                prof: dict = {}
                db = open_view(arch)
                post_comments = 0
                if db is not None:
                    try:
                        m = view_meta(db)
                        prof = m.get("profile") or {}
                        info.update(comments=m["counts"]["my_comments"], posts=m["counts"]["posts"], built=True,
                                    built_at=m.get("built_at"))
                        post_comments = m["counts"].get("post_comments") or 0
                    finally:
                        db.close()
                elif arch.raw_profile().exists():
                    prof = read_json_gz(arch.raw_profile())
                info["name"] = prof.get("name") or d.name
                # the comment pages exist while the archive keeps comments (or still has some)
                info["comments_on"] = comments_kept(arch.settings()) or bool(info["comments"] or post_comments)
                out.append(info)
                profiles.append(prof)
            # only the few avatar files, not the whole media catalog (this runs every few seconds)
            resolver = MediaResolver(lookup(self.library, [avatar_key(p.get("avatar")) for p in profiles]))
            for info, prof in zip(out, profiles):
                info["avatar"] = avatar_src(resolver, prof.get("avatar"))
            with self.lock:
                self._accounts = (time.time(), out)
            cached = out
        return [dict(a, syncing=self.jobs.busy(a["nick"])) for a in cached]

    def account(self, nick: str) -> dict | None:
        return next((a for a in self.accounts() if a["nick"] == nick), None)

    def view(self, nick: str) -> viewer.ArchiveView | None:
        arch = self.archive(nick)
        if arch is None or not view_ready(arch):
            return None
        cfg = config_path(self.library)
        stamp = tuple(p.stat().st_mtime if p.exists() else 0 for p in (arch.view_path, cfg, arch.settings_path))
        with self.lock:
            hit = self._views.get(nick)
            if hit and hit[0] == stamp:
                return hit[1]
        v = viewer.ArchiveView(arch)
        with self.lock:
            self._views[nick] = (stamp, v)
            self._drop_pages(nick)
        return v

    def _drop_pages(self, nick: str) -> None:
        """Cached pages of one archive (under self.lock)."""
        for k in [k for k in self._pages if k[0] == nick]:
            del self._pages[k]

    def rebuild_all_soon(self, delay: float = 5.0) -> None:
        """Rebuild data/ and md/ of every built archive once changes stop for `delay` seconds (the dislike list:
        pages apply it at once, the files for agents need a build). A build already running gets one more after it."""
        def go() -> None:
            for d in archive_dirs(self.library):
                if view_ready(Archive(d, self.library)):
                    self.jobs.submit(d.name, "render", reason="reactions", after_running=True)
        with self.lock:
            if self._rebuild_timer is not None:
                self._rebuild_timer.cancel()
            self._rebuild_timer = threading.Timer(delay, go)
            self._rebuild_timer.daemon = True
            self._rebuild_timer.start()

    def invalidate(self, nick: str | None = None) -> None:
        with self.lock:
            self._accounts = None
            if nick is None:
                self._views.clear()
                self._pages.clear()
            else:
                self._views.pop(nick, None)
                self._drop_pages(nick)

    def cached(self, key: tuple, build: Any) -> str | None:
        with self.lock:
            if key in self._pages:
                self._pages.move_to_end(key)
                return self._pages[key]
        html_ = build()
        if html_ is not None:
            with self.lock:
                self._pages[key] = html_
                while len(self._pages) > 60:
                    self._pages.popitem(last=False)
        return html_

    def current_nick(self, nick: str | None = None) -> str | None:
        """Archive shown in the app bar: the page's own, else the one in the URL, else the last opened, else the first."""
        accs = self.accounts()
        for cand in (nick, getattr(self.req, "nick", None), getattr(self.req, "last", None)):
            if cand and any(a["nick"] == cand for a in accs):
                return cand
        return accs[0]["nick"] if accs else None

    def page(self, title: str, body: str, nick: str | None = None, active: str = "", wide: bool = False,
             extra: str = "", bare: bool = False) -> str:
        cur = self.current_nick(nick)
        current = self.account(cur) if cur else None
        return self.shell.page(title, body, current=current, accounts=self.accounts(), active=active, wide=wide,
                               extra=extra, bare=bare)


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = f"LDTF/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet console
        pass

    # ------------------------------------------------------------------ helpers
    def _host_name(self) -> str:
        try:
            return (urllib.parse.urlsplit("//" + (self.headers.get("Host") or "")).hostname or "").lower()
        except ValueError:
            return ""

    def _local_host(self) -> bool:
        """On this computer only a local Host is served (DNS rebinding); beyond it the address can be anything."""
        return self.app.remote or self._host_name() in config.LOOPBACK

    def _same_origin(self) -> bool:
        """Other sites in the browser can't trigger actions: Origin, when sent, is this server."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            host = urllib.parse.urlsplit(origin).hostname
        except ValueError:
            return False
        if self.app.remote:
            return bool(host) and host.lower() == self._host_name()
        return host in config.LOOPBACK

    def send_body(self, code: int, body: bytes, ctype: str, headers: dict | None = None, gz: bool = False) -> None:
        if gz and len(body) > 8192 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            headers = dict(headers or {}, **{"Content-Encoding": "gzip"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def html(self, text: str, code: int = 200, headers: dict | None = None) -> None:
        if self.command == "GET" and (self.headers.get("Sec-Fetch-Mode") or "navigate") == "navigate":
            note = self.app.take_flash(urllib.parse.urlsplit(self.path).path)
            if note:   # where the page keeps its notices, else at the top of the page
                text = (text.replace(FLASH, note, 1) if FLASH in text else
                        re.sub(r"(<main[^>]*>)", lambda m: m.group(1) + note, text, count=1))
        text = text.replace(FLASH, "")
        h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
             "X-Frame-Options": "DENY"}
        h.update(headers or {})
        self.send_body(code, text.encode("utf-8"), "text/html; charset=utf-8", h, gz=True)

    def wants_json(self) -> bool:
        """A settings page saving one control (app.js): the answer is JSON, not a redirect."""
        return "application/json" in (self.headers.get("Accept") or "")

    def json(self, obj: Any, code: int = 200) -> None:
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})

    def redirect(self, url: str, code: int = 303, flash: str = "") -> None:
        """`flash`: HTML the next page view of `url` shows once (a snackbar or a banner) - the outcome of a form."""
        if flash:
            self.app.flash(urllib.parse.urlsplit(url).path, flash)
        self.send_response(code)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def not_found(self, msg: str = "Страница не найдена") -> None:
        body = empty_state("search_off", msg, "Проверьте адрес или вернитесь на главную.",
                           btn("На главную", ic="home", href="/"), tag="h1")
        self.html(self.app.page("Не найдено", body), 404)

    def error_page(self, e: Exception, code: int = 500) -> None:
        body = empty_state("error", "Ошибка", f"<code>{E(type(e).__name__)}: {E(str(e))}</code>",
                           btn("На главную", ic="home", href="/"), tag="h1")
        self.html(self.app.page("Ошибка", body), code)

    def start_request(self) -> None:
        self.app.req.last = self.cookie("last")
        self.app.req.nick = None

    def cookie(self, name: str) -> str | None:
        c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return c[name].value if name in c else None

    def form(self) -> dict[str, list[str]]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 2_000_000)).decode("utf-8", "replace") if n else ""
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    # ------------------------------------------------------------------ static
    def static(self, base: Path, rel: str, immutable: bool) -> None:
        rel = urllib.parse.unquote(rel)
        try:
            path = (base / rel).resolve()
            path.relative_to(base.resolve())
        except (ValueError, OSError):
            return self.not_found()
        if not path.is_file():
            return self.not_found("Файл не найден")
        ctype = MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        cache = "public, max-age=31536000, immutable" if immutable else "public, max-age=86400"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionError, OSError):
                    return
                left -= len(chunk)

    # ------------------------------------------------------------------ GET
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._local_host():
            return self.send_body(403, b"forbidden", "text/plain")
        self.start_request()
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        qs = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        try:
            if path.startswith("/assets/"):
                return self.static(ASSETS, path[len("/assets/"):], immutable=bool(qs("v")))
            if path.startswith("/media/"):
                return self.static(self.app.library / "media", path[len("/media/"):], immutable=True)
            if path == "/api/ping":
                return self.json({"app": "ldtf", "version": __version__, "pid": os.getpid(),
                                  "library": self.app.library_id})
            if path == "/api/jobs":
                return self.json([j.snapshot() for j in self.app.jobs.active()])
            m = re.fullmatch(r"/api/jobs/(\d+)", path)
            if m:
                j = self.app.jobs.get(int(m.group(1)))
                if not j:
                    return self.json({"error": "нет такого задания"}, 404)
                snap = j.snapshot()
                if j.kind == "sync" and j.state == RUNNING:   # the sync tab tells when DTF asks everyone to wait
                    snap["net"] = self.app.net.state()["api"]
                return self.json(snap)
            if path == "/api/diagnostics":
                return self.json(self.app.diag)
            if path == "/":
                last = self.cookie("last")
                accs = self.app.accounts()
                if last and any(a["nick"] == last for a in accs):
                    return self.redirect(Links(last).home(), 302)
                if accs:
                    return self.redirect(Links(accs[0]["nick"]).home(), 302)
                return self.redirect("/add", 302)
            if path == "/archives":
                return self.html(app_pages.archives_page(self.app))
            if path == "/add":
                return self.html(app_pages.add_page(self.app))
            if path == "/jobs":
                act = self.app.jobs.active()
                return self.redirect(Links(act[0].nick).sync() if act else "/archives", 302)
            if path == "/diagnostics":
                return self.html(app_pages.diagnostics_page(self.app))
            if path == "/app":
                return self.html(app_pages.app_settings_page(self.app))
            if path == "/app/reactions":
                return self.html(app_pages.app_reactions_page(self.app))
            m = re.fullmatch(r"/u/([^/]+)(/.*)?", path)
            if m:
                return self.archive_get(urllib.parse.unquote(m.group(1)), m.group(2) or "/", qs, q)
            return self.not_found()
        except (ConnectionError, BrokenPipeError):
            return
        except Exception as e:  # noqa: BLE001
            log.exception(f"[app] ошибка страницы {self.path}")
            return self.error_page(e)

    def archive_get(self, nick: str, sub: str, qs: Any, q: dict | None = None) -> None:
        q = q or {}
        app = self.app
        arch = app.archive(nick)
        if arch is None:
            return self.not_found("Такого архива нет")
        app.req.nick = nick
        cookie = {"Set-Cookie": f"last={nick}; Path=/; Max-Age=31536000; SameSite=Lax"}
        if sub == "/sync":
            return self.html(app_pages.sync_page(app, nick), headers=cookie)
        if sub == "/settings":
            return self.html(app_pages.settings_page(app, nick), headers=cookie)
        if sub == "/reactions":   # LDTF 1.3 kept a dislike list per archive: now one for all, in the app settings
            return self.redirect("/app/reactions", 302)
        v = app.view(nick)
        if v is None:  # first sync still running / never built
            return self.redirect(Links(nick).sync(), 302)
        if ((sub == "/comments" or sub.startswith(("/c/", "/go/c/")))
                and not (app.account(nick) or {}).get("comments_on", True)):
            return self.redirect(Links(nick).home(), 302)   # an archive of posts only has no comment pages
        m = re.fullmatch(r"/go/c/(\d+)", sub)
        if m:
            target = viewer.page_go(v, int(m.group(1)))
            return self.redirect(target, 302) if target else self.not_found("Комментарий не найден в архиве")
        guard = ((app.account(nick) or {}).get("guard") or {}).get("ts")   # the page chrome shows the guard banner
        key = (nick, sub, qs("q"), qs("t"), qs("y"), qs("s"), qs("page"), qs("exact"), tuple(q.get("g") or ()), guard)

        def build() -> str | None:
            res, active, wide = None, "", False
            if sub in ("/", ""):
                res, active, wide = viewer.page_home(v), "index", True
            elif sub == "/posts":
                res, active, wide = viewer.page_posts(v), "posts", True
            elif sub == "/comments":
                res, active, wide = viewer.page_calendar(v), "comments", True
            elif sub == "/donations":
                res, active = insights.page_donations(v), "index"
            elif sub == "/search":
                page = int(qs("page", "1") or 1) if qs("page", "1").isdigit() else 1
                gs = q.get("g") or ["1"]  # checkbox + hidden fallback: "1" when checked
                res, active = viewer.page_search(v, qs("q"), qs("t"), qs("y"), qs("s", "rank"), page,
                                                 exact=qs("exact") == "1", group="1" in gs,
                                                 rebuild=lambda action: app.shell.form(action, btn("Пересобрать", "text",
                                                                                                   "restart_alt"))), "search"
            else:
                mm = re.fullmatch(r"/p/(\d+)", sub)
                if mm:
                    res, active = viewer.page_post(v, int(mm.group(1))), "posts"
                mm = re.fullmatch(r"/c/(\d{4}-\d{2})", sub)
                if mm:
                    page = int(qs("page", "1")) if qs("page", "1").isdigit() else 1
                    res, active = viewer.page_month(v, mm.group(1), page), "comments"
            if res is None:
                return None
            title, body = res
            return app.page(title, body, nick, active, wide)

        cacheable = sub != "/search"
        html_ = app.cached(key, build) if cacheable else build()
        if html_ is None:
            return self.not_found()
        return self.html(html_, headers=cookie)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._local_host():
            return self.send_body(403, b"forbidden", "text/plain")
        if not self._same_origin():
            return self.send_body(403, "запрос с чужого сайта отклонён".encode(), "text/plain; charset=utf-8")
        if urllib.parse.urlsplit(self.path).path.startswith("/api/"):
            return self.api_post()
        self.start_request()
        mm = re.match(r"/u/([^/]+)/", urllib.parse.urlsplit(self.path).path)
        if mm:
            self.app.req.nick = urllib.parse.unquote(mm.group(1))
        f = self.form()
        if not secrets.compare_digest((f.get("_csrf") or [""])[0].encode(), self.app.csrf.encode()):
            if self.wants_json():
                return self.json({"error": "Страница устарела — обновите её и повторите.", "stale": True}, 403)
            body = empty_state("lock", "Сессия устарела", "Приложение перезапускалось. Обновите страницу и повторите действие.",
                               tag="h1")
            return self.html(self.app.page("Сессия устарела", body), 403)
        val = lambda k, d="": (f.get(k) or [d])[0].strip()  # noqa: E731
        try:
            return app_pages.handle_post(self, self.app, urllib.parse.urlsplit(self.path).path, f, val)
        except Exception as e:  # noqa: BLE001
            log.exception(f"[app] ошибка действия {self.path}")
            if self.wants_json():
                return self.json({"error": f"Ошибка: {type(e).__name__}: {e}"}, 500)
            return self.error_page(e)


    def api_post(self) -> None:
        """JSON job API for the CLI: POST /api/jobs {nick, kind, full, user, accept}; POST /api/jobs/<id>/cancel."""
        if not secrets.compare_digest(self.headers.get("X-LDTF-Token") or "", self.app.token):
            return self.json({"error": "нужен токен из app.run.json"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}") if n else {}
        except ValueError:
            return self.json({"error": "ожидается JSON"}, 400)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/jobs":
            nick = str(body.get("nick") or "")
            if not NICK_RE.match(nick):
                return self.json({"error": "неверное имя архива"}, 400)
            kind = str(body.get("kind") or "sync")
            if kind not in KINDS:
                return self.json({"error": f"неизвестный вид задания: {kind}"}, 400)
            (self.app.library / nick / ".state").mkdir(parents=True, exist_ok=True)
            params = {"reason": "cli", "full": bool(body.get("full")), "accept": bool(body.get("accept"))}
            if body.get("user"):
                params["user"] = str(body["user"])
            job = self.app.jobs.submit(nick, kind, **params)
            self.app.invalidate()
            return self.json(job.snapshot())
        m = re.fullmatch(r"/api/jobs/(\d+)/cancel", path)
        if m:
            return self.json({"ok": self.app.jobs.cancel(int(m.group(1)))})
        return self.json({"error": "нет такого метода"}, 404)


def _free_port(start: int, host: str = "127.0.0.1") -> int:
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise OSError("нет свободного порта")


def _ping(port: int) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            d = json.load(r)
            return d if d.get("app") in ("ldtf", "dtf-archive") else None
    except Exception:  # noqa: BLE001
        return None


def form_secret(library: Path) -> str:
    """The CSRF token of the forms, made once per library: pages opened before a restart of LDTF still save."""
    p = library / ".state" / "app.secret"
    try:
        token = p.read_text(encoding="utf-8").strip()
        if len(token) >= 24:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        atomic_write_text(p, token + "\n")
    except OSError as e:   # a read-only library: the token lives until the app stops
        log.warning(f"[app] не удалось сохранить {p}: {e}")
    return token


def run_file(library: Path) -> Path:
    return library / ".state" / "app.run.json"


def find_running(library: Path, port: int = DEFAULT_PORT) -> dict | None:
    """A LDTF already serving this library: {"url", "port", "token"?}; checks the run file, then the default port.
    Another library's LDTF on that port (another copy of the app, a test) is not this one."""
    lib = library_id(library)
    rf = run_file(library.resolve())
    try:
        info = json.loads(rf.read_text(encoding="utf-8"))
        ping = _ping(int(info["port"]))
        if ping and ping.get("library", lib) == lib:   # LDTF before 1.4 doesn't say: its run file is enough
            return {**info, "url": f"http://127.0.0.1:{int(info['port'])}/"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    ping = _ping(port)
    if ping and ping.get("library") == lib:
        return {"port": port, "url": f"http://127.0.0.1:{port}/"}
    return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets a second server take a port that is in use: the port must be ours alone
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr: tuple, handler: Any):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A request the browser dropped (a lazy picture scrolled away, a closed tab) is not an error of the app."""
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Runtime:
    """The running app: HTTP server thread + scheduler; `stop()` shuts everything down gracefully.
    `port`: None takes DEFAULT_PORT or the next free one; a port given explicitly is used as is (Docker, servers)."""

    def __init__(self, library: Path, port: int | None = None, scheduler_delay: float = 10.0,
                 host: str = "127.0.0.1"):
        self.port = port if port else _free_port(DEFAULT_PORT, host)
        self.app = App(library, self.port, host)
        self.app.scheduler.first_delay = scheduler_delay
        Handler.app = self.app
        self.httpd = Server((host, self.port), Handler)
        shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else (f"[{host}]" if ":" in host else host)
        self.url = f"http://{shown}:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                       name="http", daemon=True)
        self.stopped = threading.Event()

    def start(self, scheduler: bool = True) -> "Runtime":
        _clean_media_tmp(self.app.library)
        self.thread.start()
        self.app.scheduling = scheduler
        if scheduler:
            self.app.scheduler.start()
        write_json(run_file(self.app.library), {"pid": os.getpid(), "port": self.port, "token": self.app.token,
                                                "version": __version__, "started": int(time.time())})
        self._catch_up()
        log.info(f"LDTF {__version__} работает: {self.url} (архивы: {self.app.library})")
        return self

    def _catch_up(self) -> None:
        """Work left from the previous run: comments of an archive switched to posts only, archives built by an older
        LDTF (rebuilt one at a time, unless a sync is due now - it builds anyway), a postponed media cleanup."""
        now = time.time()
        for d in archive_dirs(self.app.library):
            arch = Archive(d, self.app.library)
            try:
                if pending_drop(arch):
                    self.app.jobs.submit(d.name, "purge", reason="manual")
                elif view_outdated(arch):
                    due = self.app.next_run(d.name)
                    if due is None or due > now + 60:
                        self.app.jobs.submit(d.name, "render", reason="update")
            except Exception:  # noqa: BLE001 - one damaged archive must not stop the app
                log.exception(f"[app] @{d.name}: проверка при запуске")
        if gc_pending(self.app.library).exists():   # a cleanup postponed while syncs were running
            self.app.jobs.want_gc()

    def stop(self, timeout: float = 20.0) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        log.info("LDTF останавливается: синхронизации сохраняют прогресс…")
        self.app.scheduler.stop()
        self.app.jobs.stop(timeout)
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            rf = run_file(self.app.library)
            if json.loads(rf.read_text(encoding="utf-8")).get("pid") == os.getpid():
                rf.unlink()
        except (OSError, ValueError):
            pass
        log.info("LDTF остановлен.")


def _clean_media_tmp(library: Path) -> None:
    """Partial downloads left by crashed syncs (each archive has its own folder under media/.tmp)."""
    import shutil
    tmp = library / "media" / ".tmp"
    if not tmp.is_dir():
        return
    cutoff = time.time() - 24 * 3600
    for p in tmp.iterdir():
        try:
            if p.stat().st_mtime < cutoff or p.is_file():
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            pass


def port_problem(host: str, port: int | None, e: OSError) -> str:
    if port:
        return (f"Не удалось занять {host}:{port} ({e}). Порт занят другой программой или другой копией LDTF — "
                f"укажите другой: --port или LDTF_PORT.")
    return f"Не удалось запустить сервер на {host}: {e}"


def check_writable(library: Path) -> str | None:
    """Why LDTF can't keep its archives in this folder (a Docker volume made by root, a read-only disk), or None."""
    probe = library / ".state" / ".write-test"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as e:
        hint = ""
        if os.name != "nt" and hasattr(os, "getuid"):
            hint = (f" Дайте пользователю {os.getuid()}:{os.getgid()} права на папку, например: "
                    f"sudo chown -R {os.getuid()}:{os.getgid()} <папка с данными на сервере>")
        return f"Нет записи в папку архивов {library}: {e}.{hint}"


def serve(library: Path, port: int | None = None, open_browser: bool = False, auto_sync: bool = True,
          host: str = "127.0.0.1") -> int:
    """Console mode: the server runs until Ctrl+C / SIGTERM (docker stop) / "Остановить LDTF" in the app."""
    running = find_running(library, port or DEFAULT_PORT)
    if running:
        print(f"LDTF уже запущен: {running['url']}")
        if open_browser:
            webbrowser.open(running["url"])
        return 0
    problem = check_writable(library)
    if problem:
        log.error(problem)
        return 1
    try:
        rt = Runtime(library, port, scheduler_delay=10.0, host=host).start(scheduler=auto_sync)
    except OSError as e:
        log.error(port_problem(host, port, e))
        return 1
    quit_ = threading.Event()
    rt.app.on_quit = quit_.set
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda *_: quit_.set())
        except ValueError:   # not the main thread (tests)
            pass
    log.info(f"Архивы: {rt.app.library}")
    if rt.app.remote:
        log.info("Сервер доступен и по адресу этого компьютера в сети.")
    if sys.stdin is not None and sys.stdin.isatty():
        print("Закройте это окно (или Ctrl+C), чтобы остановить приложение.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(rt.url)).start()
    try:
        while not quit_.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    log.info("Останавливаю…")
    rt.stop()
    return 0
