"""App-wide settings (archive/.state/app.json): scheduling and tray behaviour. The network budget is not a setting:
the app adapts it to DTF by itself (netpool.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .settings import SAVE_LOCK, clean, load_with_defaults, log_changes
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


def save_app_settings(library: Path, values: dict) -> dict:
    """Change the given keys (a page sends only the control that changed, the tray one toggle); the rest stay."""
    p = app_settings_path(library)
    with SAVE_LOCK:
        cur = load_app_settings(library)
        s = clean(DEFAULTS, LIMITS, values, cur)
        if s != cur or not p.exists():
            write_json(p, s)
    log_changes("приложение", cur, s)
    return s
