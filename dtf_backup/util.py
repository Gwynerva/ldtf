"""Small shared helpers: atomic file IO, gzip JSON, time formatting, logging."""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("dtf_backup")

MSK = _dt.timezone(_dt.timedelta(hours=3), "MSK")
HOUR, DAY = 3600, 86400


def setup_logging(log_file: Path | None, verbose: bool = False, app_log: Path | None = None) -> None:
    """Console (when there is one: pythonw has no stdout), an optional DEBUG file, an optional rotating app log."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(console)
    if app_log is not None:
        from logging.handlers import RotatingFileHandler
        app_log.parent.mkdir(parents=True, exist_ok=True)
        rh = RotatingFileHandler(app_log, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        rh.setLevel(logging.INFO)
        rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        log.addHandler(rh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        log.addHandler(fh)


def now_ts() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


def ts_to_dt(ts: int | float | None) -> _dt.datetime | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, MSK)


def ts_iso(ts: int | float | None) -> str | None:
    d = ts_to_dt(ts)
    return d.isoformat() if d else None


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def ts_human(ts: int | float | None) -> str:
    """27 сентября 2026, 18:44 (Moscow time, as on DTF)."""
    d = ts_to_dt(ts)
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}, {d:%H:%M}" if d else ""


def ts_date(ts: int | float | None) -> str:
    """15 мая 2019"""
    d = ts_to_dt(ts)
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}" if d else ""


def ts_month(ts: int | float) -> str:
    return ts_to_dt(ts).strftime("%Y-%m")  # type: ignore[union-attr]


def ts_year(ts: int | float) -> str:
    return ts_to_dt(ts).strftime("%Y")  # type: ignore[union-attr]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def dumps(obj: Any, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_json(path: Path, obj: Any, pretty: bool = True) -> None:
    atomic_write_text(path, dumps(obj, pretty) + "\n")


def write_json_gz(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, gzip.compress(dumps(obj).encode("utf-8"), compresslevel=6, mtime=0))


_REQUIRED: Any = object()


def read_json_gz(path: Path, default: Any = _REQUIRED) -> Any:
    """A gzipped JSON file; `default` if it does not exist (without a default a missing file is an error)."""
    if default is not _REQUIRED and not path.exists():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_jsonl_gz(path: Path, rows: Iterable[Any]) -> int:
    parts = [dumps(r) for r in rows]
    data = ("\n".join(parts) + ("\n" if parts else "")).encode("utf-8")
    atomic_write_bytes(path, gzip.compress(data, compresslevel=6, mtime=0))
    return len(parts)


def read_jsonl_gz(path: Path) -> list[Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


POSTS = ("пост", "поста", "постов")
COMMENTS = ("комментарий", "комментария", "комментариев")
FILES = ("файл", "файла", "файлов")


def plural(n: int, one: str, few: str, many: str) -> str:
    """The word for n: 1 пост, 2 поста, 5 постов (forms: POSTS, COMMENTS, ... or any three)."""
    n = abs(int(n)) % 100
    if 10 < n < 20:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def num(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ")  # no-break space: "89 675" never splits across lines


def count_label(n: int, one: str, few: str, many: str) -> str:
    """"89 675 комментариев"."""
    return f"{num(n)} {plural(n, one, few, many)}"


_TAGS = re.compile(r"<[^>]+>")


def short(text: str | None, n: int = 90) -> str:
    """One line of plain text (tags dropped), at most n characters, "…" when cut."""
    t = " ".join(_TAGS.sub(" ", text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"
