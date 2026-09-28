"""DTF (Osnova platform) API endpoints used by the backup.

All endpoints below work anonymously. They were taken from the dtf.ru web bundle
(the site calls `https://api.dtf.ru/<version>/<endpoint>`). Authenticated calls would
add a `JWTAuthorization: Bearer <access token>` header; see README for why this tool
does not do it by default.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .http import HttpClient, HttpError

API = "https://api.dtf.ru"
MEDIA = "https://leonardo.osnova.io"
SITE = "https://dtf.ru"


class ApiError(Exception):
    pass


def media_url(uuid_or_url: str) -> str:
    if uuid_or_url.startswith("http"):
        return uuid_or_url
    return f"{MEDIA}/{uuid_or_url}/"


def post_url(post_id: int) -> str:
    return f"{SITE}/{post_id}"


def comment_url(post_id: int, comment_id: int) -> str:
    return f"{SITE}/{post_id}?comment={comment_id}"


def parse_user_ident(s: str) -> tuple[str, str]:
    """'petra', '@petra', '136492', 'id136492', 'https://dtf.ru/u/136492-x', 'https://dtf.ru/petra' ->
    ('id', '136492') or ('uri', 'petra')."""
    s = s.strip()
    if s.startswith("http"):
        path = urllib.parse.urlsplit(s).path.strip("/").split("/")
        if path and path[0] in ("u", "s") and len(path) > 1:
            m = re.match(r"(\d+)", path[1])
            if m:
                return "id", m.group(1)
        s = path[0] if path and path[0] else s
    s = s.lstrip("@")
    m = re.fullmatch(r"(?:id)?(\d+)", s)
    if m:
        return "id", m.group(1)
    return "uri", s


class Dtf:
    def __init__(self, client: HttpClient):
        self.client = client

    def _get(self, version: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{API}/{version}/{endpoint}" + (f"?{q}" if q else "")
        data = self.client.get_json(url)
        if not isinstance(data, dict) or "result" not in data:
            raise ApiError(f"unexpected response for {url}: {str(data)[:200]}")
        return data["result"]

    # -- profile -----------------------------------------------------------
    def subsite(self, ident: str) -> dict:
        kind, value = parse_user_ident(ident)
        return self._get("v2.7", "subsite", {kind: value, "markdown": "false"})

    def assets(self) -> dict:
        """Catalogs used by the site: reactions [{id, type, staticUuid, animatedUuid}] and badges."""
        return self._get("v2.9", "assets")

    # -- posts -------------------------------------------------------------
    def timeline_page(self, subsite_id: int, cursor: str | None = None) -> tuple[list[dict], str | None]:
        r = self._get("v2.10", "timeline", {
            "markdown": "false", "sorting": "new", "subsitesIds": subsite_id, "cursor": cursor})
        items = [it["data"] for it in r.get("items", []) if isinstance(it, dict) and "data" in it]
        return items, r.get("cursor")

    def content(self, post_id: int) -> dict:
        return self._get("v2.10", "content", {"id": post_id, "markdown": "false"})

    # -- comments ----------------------------------------------------------
    def post_comments(self, post_id: int) -> list[dict]:
        """Whole comment tree of a post (all levels). Note: `firstLoad=true` would return only 2 levels."""
        r = self._get("v2.10", "comments", {"sorting": "date", "contentId": post_id})
        return r.get("items", [])

    def comment_branch(self, comment_id: int) -> list[dict]:
        """The whole root-level branch (thread) that contains the comment."""
        r = self._get("v2.10", "comments", {"commentId": comment_id})
        return r.get("items", [])

    def comment_replies(self, comment_id: int) -> list[dict]:
        r = self._get("v2.5", "comments", {"commentId": comment_id, "onlyReplies": "true"})
        return r.get("items", [])

    def user_comments_page(self, subsite_id: int, last_id: int | None = None,
                           last_sorting_value: int | None = None) -> tuple[list[dict], int | None, int | None]:
        """Comments written by the user (newest first), 30 per page. Pass last_id=999999999 and
        last_sorting_value=<unix ts> to start from an arbitrary point in time."""
        r = self._get("v2.5", "comments", {
            "sorting": "new", "subsiteId": subsite_id,
            "lastId": last_id, "lastSortingValue": last_sorting_value})
        items = r.get("items", [])
        lid = r.get("lastId")
        lsv = r.get("lastSortingValue")
        if items and lid is None:
            lid = items[-1]["id"]
        if items and lsv is None:
            lsv = items[-1]["date"]
        return items, lid, lsv


def is_not_found(e: Exception) -> bool:
    return isinstance(e, HttpError) and e.status in (403, 404, 410, 451)
