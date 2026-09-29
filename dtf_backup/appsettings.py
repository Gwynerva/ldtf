"""App-wide settings (archive/.state/app.json): scheduling and tray behaviour. The network budget is not a setting:
the app adapts it to DTF by itself (netpool.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .settings import clean, load_with_defaults
from .util import write_json

DEFAULTS: dict[str, Any] = {
    "autosync": True,        # scheduled syncs (a global pause switch)
    "notify": True,          # tray notifications about finished / failed syncs
    "open_browser": True,    # open the browser when LDTF is started by hand
}
LIMITS: dict[str, tuple] = {}


def app_settings_path(library: Path) -> Path:
    return library / ".state" / "app.json"


def load_app_settings(library: Path) -> dict:
    return load_with_defaults(app_settings_path(library), DEFAULTS, LIMITS)


def save_app_settings(library: Path, values: dict, partial: bool = False) -> dict:
    """`partial`: update only the given keys (tray toggles); otherwise a full form (unchecked = False)."""
    cur = load_app_settings(library)
    if partial:
        values = {**cur, **values}
    s = clean(DEFAULTS, LIMITS, values, cur)
    p = app_settings_path(library)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, s)
    return s
