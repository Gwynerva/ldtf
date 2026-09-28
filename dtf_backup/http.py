"""Fault-tolerant HTTP client built on http.client (stdlib only).

* Persistent keep-alive connections, one per (thread, host). Opening a fresh TLS
  connection for every request is what triggers DTF's connection blocking, while
  reusing connections sustains 10+ rps without errors.
* An adaptive per-host limiter caps concurrent requests. On a network error / 429 / 5xx
  it halves the allowed concurrency and pauses everyone (exponential backoff,
  Retry-After aware). After a streak of successes it slowly restores concurrency.
  Too many consecutive failures trip a fuse: FatalNetworkError, sync stops gracefully.
  A shared limiter (the app's NetPool) cools the fuse down after a while instead of staying stopped.
* Waiting requests are served first-come-first-served, so jobs sharing one limiter get fair turns.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import ssl
import threading
import time
from collections import deque
import urllib.parse
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .util import log

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class FatalNetworkError(Exception):
    """The network looks broken or we are blocked: stop and let the user resume later."""


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes | None = None):
        self.status = status
        self.url = url
        self.body = body or b""
        snippet = self.body[:200].decode("utf-8", "replace")
        super().__init__(f"HTTP {status} for {url}: {snippet}")


class AdaptiveLimiter:
    def __init__(self, name: str, max_permits: int, base_backoff: float = 15.0,
                 max_backoff: float = 600.0, fuse: int = 10, recover_after: int = 40,
                 rate: float | None = None, max_rate: float | None = None, fuse_cooldown: float | None = None):
        self.name = name
        # None: a tripped fuse stops this limiter for good (one CLI run); else it reopens after the cooldown
        self.fuse_cooldown = fuse_cooldown
        self.fused_until = 0.0
        self._waiting: deque[object] = deque()
        # Optional requests-per-second pacing: lowered on HTTP 429, raised slowly after successes.
        self.rate = rate
        self.max_rate = max_rate or rate
        self.next_slot = 0.0
        self.max = max(1, max_permits)
        self.limit = self.max
        self.in_flight = 0
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.fuse = fuse
        self.recover_after = recover_after
        self.cond = threading.Condition()
        self.backoff_until = 0.0
        self.fail_streak = 0
        self.ok_streak = 0
        self.stopped = False
        self.requests = 0
        self.failures = 0

    def acquire(self, stop: Callable[[], bool] | None = None) -> None:
        """Wait for a permit (FIFO). `stop` lets one user of a shared limiter give up without affecting others."""
        ticket = object()
        with self.cond:
            self._waiting.append(ticket)
            try:
                while True:
                    if self.stopped or (stop is not None and stop()):
                        raise FatalNetworkError(f"{self.name}: остановлено")
                    now = time.monotonic()
                    if self.fused_until:
                        if now < self.fused_until:
                            raise FatalNetworkError(f"{self.name}: DTF ограничил запросы, пауза ещё "
                                                    f"{int(self.fused_until - now) // 60 + 1} мин")
                        self.fused_until, self.fail_streak = 0.0, 0
                        log.info(f"[{self.name}] пауза после блокировки закончилась, продолжаю осторожно")
                    if now < self.backoff_until:
                        self.cond.wait(min(self.backoff_until - now, 5.0))
                        continue
                    if self._waiting[0] is not ticket:  # first come, first served
                        self.cond.wait(1.0)
                        continue
                    if self.in_flight < self.limit:
                        if self.rate:
                            if now < self.next_slot:
                                self.cond.wait(self.next_slot - now)
                                continue
                            self.next_slot = max(now, self.next_slot) + 1.0 / self.rate
                        self.in_flight += 1
                        return
                    self.cond.wait(1.0)
            finally:
                try:
                    self._waiting.remove(ticket)
                except ValueError:
                    pass
                self.cond.notify_all()

    def release(self, ok: bool, retry_after: float | None = None, reason: str = "") -> None:
        with self.cond:
            self.in_flight -= 1
            self.requests += 1
            if ok:
                self.fail_streak = 0
                self.ok_streak += 1
                if self.limit < self.max and self.ok_streak >= self.recover_after:
                    self.limit += 1
                    self.ok_streak = 0
                    log.info(f"[{self.name}] восстанавливаю параллельность до {self.limit}")
                elif self.rate and self.max_rate and self.rate < self.max_rate and self.ok_streak >= 200:
                    self.rate = min(self.max_rate, self.rate * 1.1)
                    self.ok_streak = 0
                    log.debug(f"[{self.name}] темп {self.rate:.1f} запр/с")
            else:
                self.failures += 1
                self.ok_streak = 0
                now = time.monotonic()
                # Several in-flight requests usually fail together: escalate once per window.
                if self.rate and "429" in reason:
                    self.rate = max(1.0, self.rate * 0.7)
                if now >= self.backoff_until:
                    self.fail_streak += 1
                    self.limit = max(1, self.limit // 2)
                    delay = retry_after if retry_after else min(
                        self.max_backoff, self.base_backoff * 2 ** (self.fail_streak - 1))
                    self.backoff_until = now + delay
                    pace = f", темп {self.rate:.1f} запр/с" if self.rate else ""
                    log.warning(f"[{self.name}] ошибка ({reason}); пауза {delay:.0f} с, "
                                f"параллельность {self.limit}{pace}, серия ошибок {self.fail_streak}/{self.fuse}")
                    if self.fail_streak >= self.fuse:
                        if self.fuse_cooldown:
                            self.fused_until = now + self.fuse_cooldown
                            self.limit = 1
                            log.error(f"[{self.name}] слишком много ошибок подряд: пауза "
                                      f"{self.fuse_cooldown / 60:.0f} мин для всех синхронизаций")
                        else:
                            self.stopped = True
            self.cond.notify_all()

    def configure(self, max_permits: int, rate: float | None = None) -> None:
        """New limits from the app settings, applied to running syncs too."""
        with self.cond:
            self.max = max(1, max_permits)
            self.limit = min(self.limit, self.max) if self.fail_streak else self.max
            if rate:
                self.rate = min(self.rate or rate, rate) if self.fail_streak else rate
                self.max_rate = rate
            self.cond.notify_all()

    def state(self) -> dict:
        with self.cond:
            now = time.monotonic()
            return {"name": self.name, "limit": self.limit, "max": self.max, "in_flight": self.in_flight,
                    "rate": round(self.rate, 1) if self.rate else None, "waiting": len(self._waiting),
                    "backoff": max(0, int(self.backoff_until - now)), "fused": max(0, int(self.fused_until - now)),
                    "requests": self.requests, "failures": self.failures}


class LimiterLease:
    """One job's handle on a shared limiter: stopping it releases only this job's waiting threads."""

    def __init__(self, shared: AdaptiveLimiter):
        self.shared = shared
        self.name = shared.name
        self.stopped = False

    @property
    def cond(self) -> threading.Condition:
        return self.shared.cond

    def acquire(self) -> None:
        self.shared.acquire(stop=lambda: self.stopped)

    def release(self, ok: bool, retry_after: float | None = None, reason: str = "") -> None:
        self.shared.release(ok, retry_after, reason)


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes = b""
    sha256: str | None = None
    size: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _retry_after(headers: dict[str, str]) -> float | None:
    v = headers.get("retry-after")
    if not v:
        return None
    try:
        return max(1.0, min(float(v), 1800.0))
    except ValueError:
        return None


class HttpClient:
    def __init__(self, limiters: dict[str, Any], timeout: float = 40.0,
                 attempts: int = 8, extra_headers: dict[str, str] | None = None):
        self.limiters = limiters
        self.timeout = timeout
        self.attempts = attempts
        self.extra_headers = extra_headers or {}
        self._local = threading.local()
        self._ssl = ssl.create_default_context()

    # -- connections -------------------------------------------------------
    def _conns(self) -> dict[str, http.client.HTTPSConnection]:
        c = getattr(self._local, "conns", None)
        if c is None:
            c = self._local.conns = {}
        return c

    def _get_conn(self, host: str) -> http.client.HTTPSConnection:
        conns = self._conns()
        conn = conns.get(host)
        if conn is None:
            conn = http.client.HTTPSConnection(host, timeout=self.timeout, context=self._ssl)
            conns[host] = conn
        return conn

    def _drop_conn(self, host: str) -> None:
        conn = self._conns().pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        for host in list(self._conns()):
            self._drop_conn(host)

    def _limiter(self, host: str) -> Any:
        lim = self.limiters.get(host) or self.limiters.get("*")
        assert lim is not None, f"no limiter for {host}"
        return lim

    # -- single exchange ---------------------------------------------------
    def _exchange(self, url: str, headers: dict[str, str],
                  sink: Callable[[http.client.HTTPResponse, Response], None] | None) -> Response:
        """One request on a persistent connection. Follows redirects. Raises on network errors."""
        for _ in range(6):
            u = urllib.parse.urlsplit(url)
            host = u.netloc
            path = u.path + ("?" + u.query if u.query else "")
            conn = self._get_conn(host)
            h = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Connection": "keep-alive"}
            h.update(self.extra_headers)
            h.update(headers)
            try:
                conn.request("GET", path or "/", headers=h)
                resp = conn.getresponse()
                rh = {k.lower(): v for k, v in resp.getheaders()}
                if resp.status in (301, 302, 303, 307, 308) and "location" in rh:
                    resp.read()
                    url = urllib.parse.urljoin(url, rh["location"])
                    continue
                r = Response(status=resp.status, url=url, headers=rh)
                if sink is not None and resp.status in (200, 206):
                    sink(resp, r)
                else:
                    body = resp.read()
                    if rh.get("content-encoding") == "gzip":
                        body = gzip.decompress(body)
                    r.body = body
                if rh.get("connection", "").lower() == "close":
                    self._drop_conn(host)
                return r
            except BaseException:
                self._drop_conn(host)
                raise
        raise HttpError(310, url, b"too many redirects")

    def fetch(self, url: str, headers: dict[str, str] | None = None,
              sink: Callable[[http.client.HTTPResponse, Response], None] | None = None,
              on_retry: Callable[[], None] | None = None) -> Response:
        """GET with retries and adaptive limiting. Raises HttpError for final non-2xx."""
        host = urllib.parse.urlsplit(url).netloc
        limiter = self._limiter(host)
        last_exc: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            limiter.acquire()
            try:
                r = self._exchange(url, headers or {}, sink)
            except (OSError, http.client.HTTPException, ssl.SSLError, EOFError, ValueError, zlib.error) as e:
                limiter.release(False, reason=f"{type(e).__name__}: {e}"[:160])
                last_exc = e
                if on_retry:
                    on_retry()
                continue
            except BaseException:
                limiter.release(True)
                raise
            if r.status in RETRIABLE_STATUS:
                limiter.release(False, _retry_after(r.headers), reason=f"HTTP {r.status}")
                last_exc = HttpError(r.status, url, r.body)
                if on_retry:
                    on_retry()
                continue
            limiter.release(True)
            if r.status >= 400:
                raise HttpError(r.status, url, r.body)
            return r
        assert last_exc is not None
        if isinstance(last_exc, HttpError):
            raise last_exc
        raise HttpError(0, url, f"{type(last_exc).__name__}: {last_exc}".encode())

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        h = {"Accept": "application/json"}
        h.update(headers or {})
        for attempt in range(3):
            r = self.fetch(url, h)
            try:
                return json.loads(r.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A proxy/anti-bot page instead of JSON: treat as transient.
                log.warning(f"не-JSON ответ от {url} (попытка {attempt + 1}): {r.body[:120]!r}")
                time.sleep(10 * (attempt + 1))
        raise HttpError(r.status, url, b"invalid JSON: " + r.body[:200])

    def download(self, url: str, dest: Path, byte_range: str | None = None) -> Response:
        """Stream a file to `dest` (overwritten), computing sha256 on the fly."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        def sink(resp: http.client.HTTPResponse, r: Response) -> None:
            h = hashlib.sha256()
            size = 0
            gz = r.headers.get("content-encoding") == "gzip"
            with open(dest, "wb") as f:
                if gz:
                    data = gzip.decompress(resp.read())
                    f.write(data)
                    h.update(data)
                    size = len(data)
                else:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
            expected = r.headers.get("content-length")
            if expected is not None and not gz and int(expected) != size:
                raise http.client.IncompleteRead(b"", int(expected) - size)
            r.sha256 = h.hexdigest()
            r.size = size

        headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
        if byte_range:
            headers["Range"] = byte_range
        return self.fetch(url, headers, sink=sink)

    def fetch_range(self, url: str, byte_range: str) -> Response:
        """Small ranged GET kept in memory (for dedup probes)."""
        return self.fetch(url, {"Accept": "*/*", "Accept-Encoding": "identity", "Range": byte_range})
