"""The password of the web interface when LDTF listens beyond this computer (`serve --host 0.0.0.0`, Docker).

A session is a signed cookie: `<expiry>.<hmac>`, keyed by the library's secret and the password — changing the
password signs everyone out. Wrong passwords slow the next tries down (for every client at once: behind a proxy they
all share one address). Without a password the server refuses to listen on the network, unless LDTF_AUTH=off says a
reverse proxy in front of it checks who comes in.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING

from .icons import icon

if TYPE_CHECKING:
    from .server import App, Handler

E = html.escape
COOKIE = "ldtf_session"
MAX_AGE = 30 * 86400
# reachable without a session: the login itself, styles and icons, the health check, the MCP and job APIs (they have
# tokens of their own) and a ping that tells nothing but the app's name and version
OPEN_PATHS = ("/login", "/healthz", "/api/ping")
OPEN_PREFIXES = ("/assets/",)


class Gate:
    def __init__(self, app: "App", password: str):
        self.app = app
        self.key = hashlib.sha256((app.csrf + "\0" + password).encode("utf-8")).digest()
        self.password = password
        self.lock = threading.Lock()
        self.fails = 0
        self.until = 0.0

    # ------------------------------------------------------------------ sessions
    def _sig(self, exp: int) -> str:
        return hmac.new(self.key, str(exp).encode(), hashlib.sha256).hexdigest()

    def make(self) -> str:
        exp = int(time.time()) + MAX_AGE
        return f"{exp}.{self._sig(exp)}"

    def valid(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        exp_s, sig = value.split(".", 1)
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        return exp > time.time() and hmac.compare_digest(sig, self._sig(exp))

    # ------------------------------------------------------------------ passwords
    def wait_left(self) -> int:
        with self.lock:
            return max(0, int(self.until - time.time() + 0.999))

    def check(self, password: str) -> bool:
        with self.lock:
            if time.time() < self.until:
                return False
            ok = hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8"))
            if ok:
                self.fails = 0
                return True
            self.fails += 1
            if self.fails >= 5:   # 1 s, 2 s, 4 s ... up to 5 minutes
                self.until = time.time() + min(300, 2 ** (self.fails - 5))
            return False

    @staticmethod
    def exempt(path: str) -> bool:
        return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or path == "/mcp"


def cookie_header(h: "Handler", value: str, max_age: int = MAX_AGE) -> str:
    secure = "; Secure" if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
    return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"


def safe_next(target: str) -> str:
    """Only a path of this site (no //other.host)."""
    t = target or "/"
    return t if t.startswith("/") and not t.startswith("//") else "/"


def login_page(app: "App", nxt: str = "/", error: str = "") -> str:
    from .ui import Links
    err = f'<p class="login-err" role="alert">{icon("error", fill=True)}<span>{E(error)}</span></p>' if error else ""
    body = (f'<form class="card login" method="post" action="/login">'
            f'<img class="login-logo" src="{Links.asset("brand/ldtf-128.png")}" alt="" width="64" height="64">'
            f'<h1>LDTF</h1><p class="muted">Локальный архив DTF. Введите пароль, заданный при запуске сервера.</p>{err}'
            f'<input type="hidden" name="next" value="{E(safe_next(nxt))}">'
            f'<label class="field"><span class="field-l">Пароль</span>'
            f'<input type="password" name="password" autocomplete="current-password" autofocus required></label>'
            f'<button class="btn" type="submit">{icon("login")}Войти</button></form>')
    return app.page("Вход", body, bare=True)


def handle_login(h: "Handler", app: "App") -> None:
    gate: Gate = app.gate
    n = int(h.headers.get("Content-Length") or 0)
    f = urllib.parse.parse_qs(h.rfile.read(min(n, 10_000)).decode("utf-8", "replace") if n else "")
    nxt = safe_next((f.get("next") or ["/"])[0])
    wait = gate.wait_left()
    if wait:
        return h.html(login_page(app, nxt, f"Слишком много неверных попыток — подождите {wait} с."), 429)
    if not gate.check((f.get("password") or [""])[0]):
        wait = gate.wait_left()
        msg = "Неверный пароль." + (f" Следующая попытка — через {wait} с." if wait else "")
        return h.html(login_page(app, nxt, msg), 401)
    h.send_response(303)
    h.send_header("Location", nxt)
    h.send_header("Set-Cookie", cookie_header(h, gate.make()))
    h.send_header("Content-Length", "0")
    h.end_headers()


def handle_logout(h: "Handler") -> None:
    h.send_response(303)
    h.send_header("Location", "/login")
    h.send_header("Set-Cookie", cookie_header(h, "", 0))
    h.send_header("Content-Length", "0")
    h.end_headers()
