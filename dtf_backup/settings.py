"""Per-archive settings (.state/settings.json), editable from the app; shared cleaning helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .util import write_json

DEFAULTS: dict[str, Any] = {
    # network: used by standalone CLI runs only; the app shares one budget between archives (app settings)
    "workers": 4,            # parallel API requests
    "rate": 10.0,            # API requests per second (lowered automatically on HTTP 429)
    "media_workers": 8,      # parallel media downloads
    "refresh_days": 30,      # re-check replies / counters for the last N days on every sync
    "media": True,           # download media files
    "schedule": "interval",  # automatic sync: off | interval (every N hours) | daily (at HH:MM)
    "schedule_hours": 12,
    "schedule_time": "04:00",
}
LIMITS = {"workers": (1, 12), "rate": (1.0, 30.0), "media_workers": (1, 16), "refresh_days": (0, 3650),
          "schedule_hours": (1, 24 * 7)}
CHOICES = {"schedule": ("off", "interval", "daily")}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def clean(defaults: dict, limits: dict, values: dict, base: dict | None = None, choices: dict | None = None) -> dict:
    """Validate form/JSON values. A missing bool means an unchecked checkbox; other missing keys keep `base`."""
    s = dict(base or defaults)
    for k, default in defaults.items():
        if k not in values:
            if isinstance(default, bool):
                s[k] = False
            continue
        v = values[k]
        if isinstance(default, bool):
            s[k] = str(v).lower() in ("1", "true", "on", "yes")
        elif choices and k in choices:
            if str(v) in choices[k]:
                s[k] = str(v)
        elif isinstance(default, str):
            m = TIME_RE.match(str(v).strip())
            if m:
                s[k] = f"{int(m.group(1)):02d}:{m.group(2)}"
        else:
            try:
                v = int(float(v)) if isinstance(default, int) else float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = limits.get(k, (None, None))
            if lo is not None:
                v = max(lo, min(hi, v))
            s[k] = v
    return s


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings(path: Path) -> dict:
    user = load_json(path) if path.exists() else {}
    if "auto_sync_hours" in user and "schedule" not in user:  # older versions: sync on start after N hours
        h = float(user.get("auto_sync_hours") or 0)
        user["schedule"] = "interval" if h > 0 else "off"
        user["schedule_hours"] = max(1, min(24 * 7, int(h) or 12))
    s = dict(DEFAULTS)
    s.update({k: v for k, v in clean(DEFAULTS, LIMITS, user, DEFAULTS, CHOICES).items() if k in user})
    return s


def clean_settings(values: dict, base: dict | None = None) -> dict:
    return clean(DEFAULTS, LIMITS, values, base, CHOICES)


def save_settings(path: Path, values: dict) -> dict:
    s = clean_settings(values, load_settings(path))
    write_json(path, s)
    return s
