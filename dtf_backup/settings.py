"""Per-archive settings (.state/settings.json), editable from the app; shared cleaning helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .util import log, write_json

DEFAULTS: dict[str, Any] = {
    "scope": "all",          # what the archive keeps: all (posts + comments with context) | posts (no comments at all)
    "media": "all",          # media files to download: all | posts (+ avatars, reactions; no comment media) | off
    # how far back every sync re-checks replies, counters and deleted comments (not in the app: settings.json / CLI)
    "refresh_days": 30,
    "schedule": "interval",  # automatic sync: off | interval (every N hours) | daily (at HH:MM)
    "schedule_hours": 12,
    "schedule_time": "04:00",
}
LIMITS = {"refresh_days": (0, 3650), "schedule_hours": (1, 24 * 7)}
CHOICES = {"schedule": ("off", "interval", "daily"), "media": ("all", "posts", "off"), "scope": ("all", "posts")}
# values of older versions: "media" was an on/off switch (true/false in settings.json, "1"/missing from the form)
LEGACY = {"media": {"true": "all", "1": "all", "on": "all", "yes": "all", "false": "off", "0": "off", "": "off",
                    "no": "off"}}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def clean(defaults: dict, limits: dict, values: dict, base: dict | None = None, choices: dict | None = None,
          legacy: dict | None = None) -> dict:
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
            v = str(v)
            v = (legacy or {}).get(k, {}).get(v.lower(), v)
            if v in choices[k]:
                s[k] = v
        elif isinstance(default, str):
            m = TIME_RE.match(str(v).strip())
            if m:
                s[k] = f"{int(m.group(1)):02d}:{m.group(2)}"
        else:
            try:
                v = int(float(v)) if isinstance(default, int) else float(v)
            except (TypeError, ValueError):
                continue
            s[k] = clamp(limits, k, v)
    return s


def clamp(limits: dict, key: str, v: Any) -> Any:
    lo, hi = limits.get(key, (None, None))
    return v if lo is None else max(lo, min(hi, v))


_warned: set[tuple[str, float]] = set()


def load_json(path: Path) -> dict:
    """A settings file as a dict ({} when missing or damaged: defaults apply, the damage is logged once)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        raise ValueError("не объект JSON")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        try:
            stamp = (str(path), path.stat().st_mtime)
        except OSError:
            stamp = (str(path), 0.0)
        if stamp not in _warned:
            _warned.add(stamp)
            log.warning(f"Файл настроек {path} повреждён ({e}) — используются значения по умолчанию.")
        return {}


def load_with_defaults(path: Path, defaults: dict, limits: dict, choices: dict | None = None,
                       legacy: dict | None = None, user: dict | None = None) -> dict:
    """Defaults + the valid values of the file (keys missing from the file keep their defaults)."""
    user = load_json(path) if user is None else user
    s = dict(defaults)
    s.update({k: v for k, v in clean(defaults, limits, user, defaults, choices, legacy).items() if k in user})
    return s


def load_settings(path: Path) -> dict:
    user = load_json(path)
    if "auto_sync_hours" in user and "schedule" not in user:  # older versions: sync on start after N hours
        h = float(user.get("auto_sync_hours") or 0)
        user["schedule"] = "interval" if h > 0 else "off"
        user["schedule_hours"] = max(1, min(24 * 7, int(h) or 12))
    return load_with_defaults(path, DEFAULTS, LIMITS, CHOICES, LEGACY, user=user)


def clean_settings(values: dict, base: dict | None = None) -> dict:
    return clean(DEFAULTS, LIMITS, values, base, CHOICES, LEGACY)


def save_settings(path: Path, values: dict) -> dict:
    s = clean_settings(values, load_settings(path))
    write_json(path, s)
    return s
