"""One network budget for the whole app.

DTF limits requests per IP, so every sync running in the app (and the app's own lookups: diagnostics, adding a
user) goes through the same two limiters: `api` for api.dtf.ru (requests per second + parallel requests) and
`media` for the CDN and other hosts. Jobs get leases: stopping one job never stalls the others, a 429 or a series
of errors slows everyone down, and waiting requests are served first-come-first-served, so archives share fairly.

The budget is not a user setting: the limiters lower the pace and the number of parallel requests on 429s and errors
and restore them after a run of successes, up to the ceilings below.
"""

from __future__ import annotations

from typing import Any

from .http import AdaptiveLimiter, HttpClient, LimiterLease

API_HOST = "api.dtf.ru"
API_RATE = 10.0    # requests per second to api.dtf.ru for the whole app: ~9 rps never triggered 429, ~18 rps did
API_CONN = 4       # parallel API requests
MEDIA_CONN = 8     # parallel media downloads (the CDN has no per-second limit)


class NetPool:
    def __init__(self, api_rate: float = API_RATE, api_conn: int = API_CONN, media_conn: int = MEDIA_CONN):
        self.api = AdaptiveLimiter("api", api_conn, rate=api_rate, max_rate=api_rate, fuse_cooldown=30 * 60)
        self.media = AdaptiveLimiter("media", media_conn, fuse_cooldown=15 * 60)
        self.api_conn, self.media_conn = api_conn, media_conn

    def lease(self) -> tuple[LimiterLease, LimiterLease]:
        """(api, media) limiters for one sync."""
        return LimiterLease(self.api), LimiterLease(self.media)

    def client(self, timeout: float = 20.0, attempts: int = 3) -> HttpClient:
        """A client for short app-side lookups that shares the budget with running syncs."""
        return HttpClient({API_HOST: self.api, "*": self.media}, timeout=timeout, attempts=attempts)

    def state(self) -> dict[str, Any]:
        return {"api": self.api.state(), "media": self.media.state()}
