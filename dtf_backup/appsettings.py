"""App-wide settings (archive/.state/app.json): one network budget for all archives, scheduling, tray behaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .settings import clean, load_json
from .util import write_json

DEFAULTS: dict[str, Any] = {
    "max_parallel": 2,       # archives synced at the same time
    "api_rate": 10.0,        # requests per second to api.dtf.ru for the whole app (lowered on HTTP 429)
    "api_conn": 4,           # parallel API requests for the whole app
    "media_conn": 8,         # parallel media downloads for the whole app
    "autosync": True,        # scheduled syncs (a global pause switch)
    "notify": True,          # tray notifications about finished / failed syncs
    "open_browser": True,    # open the browser when LDTF is started by hand
}
LIMITS = {"max_parallel": (1, 3), "api_rate": (1.0, 30.0), "api_conn": (1, 12), "media_conn": (1, 16)}


def app_settings_path(library: Path) -> Path:
    return library / ".state" / "app.json"


def load_app_settings(library: Path) -> dict:
    p = app_settings_path(library)
    user = load_json(p) if p.exists() else {}
    s = dict(DEFAULTS)
    s.update({k: v for k, v in clean(DEFAULTS, LIMITS, user, DEFAULTS).items() if k in user})
    return s


def save_app_settings(library: Path, values: dict, partial: bool = False) -> dict:
    """`partial`: update only the given keys (tray toggles); otherwise a full form (unchecked = False)."""
    cur = load_app_settings(library)
    if partial:
        values = {**{k: v for k, v in cur.items()}, **values}
    s = clean(DEFAULTS, LIMITS, values, cur)
    p = app_settings_path(library)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, s)
    return s
