"""Reactions: catalog (raw/assets.json.gz), icons, polarity (likes vs dislikes) and fallbacks.

DTF itself counts every reaction as +1 in `likes.counterLikes` (there are no dislikes on the new
DTF). Some reactions are clearly negative, so LDTF splits them into ▲ positive / ▼ negative using one
editable config for the whole library: archive/reactions.config.json (reactions are the same for every
post on DTF; LDTF before 1.4 kept a copy per archive — `load_config` moves it to the library once).
The catalog is the union of what every archive saved (raw/assets.json.gz).
Unknown reaction ids (not in the catalog) render as a "?" chip and are listed in the render report.
"""

from __future__ import annotations

import html
import json
import threading
from pathlib import Path
from typing import Any

from .api import media_url
from .media import raw_key
from .normalize import MediaResolver
from .util import log, read_json_gz, write_json

E = html.escape

CONFIG_NAME = "reactions.config.json"
# Chosen by the archive owner: poker face, angry, clown, poop, sad Pepe, pill, Pepe, red "V".
DEFAULT_NEGATIVE = [23, 5, 25, 39, 40, 41, 7, 28]
# Human-readable labels for the unambiguous emoji (meme pictures are left unnamed on purpose).
LABELS = {
    1: "сердце", 2: "огонь", 3: "грусть", 4: "смех до слёз", 5: "злость", 6: "удивлённый Пикачу", 7: "Пепе с прищуром",
    8: "бриллиант", 9: "попкорн", 10: "достижение", 11: "медаль-значок", 12: "платиновый кубок", 14: "кот",
    20: "чёрный кот", 21: "сосиска", 22: "крутой (в очках)", 23: "покерфейс", 24: "глаза", 25: "клоун",
    26: "нож", 28: "красная «V»", 29: "губы", 31: "баклажан", 34: "звезда", 35: "медаль", 36: "аплодисменты",
    37: "кубок", 38: "радиация", 39: "какашка", 40: "Пепе с грустными глазами", 41: "таблетка",
}


def reaction_pairs(obj: dict) -> list[tuple[Any, int]]:
    """Non-zero reaction counters [(reaction id, count)], most popular first."""
    r = obj.get("reactions") or {}
    out = []
    for x in (r.get("counters") or []) if isinstance(r, dict) else []:
        if isinstance(x, dict):
            try:
                n = int(x.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out.append((x.get("id"), n))
    out.sort(key=lambda t: -t[1])
    return out


def reactions_total(obj: dict) -> int:
    return sum(n for _, n in reaction_pairs(obj))


HELP = ("Какие реакции считать дизлайками (▼) во всех архивах. Укажите id реакций; картинки и id — в "
        "data/reactions.json любого архива и в LDTF: Настройки приложения → Реакции (там же их можно отметить мышкой).")
_CONFIG_LOCK = threading.Lock()


def config_path(library: Path) -> Path:
    return library / CONFIG_NAME


def _read_negative(path: Path) -> list | None:
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning(f"{path} не читается ({e}) — дизлайками считаются реакции по умолчанию.")
        return None
    neg = user.get("negative") if isinstance(user, dict) else None
    return clean_ids(neg) if isinstance(neg, list) else None


def clean_ids(ids: Any) -> list[int]:
    out: list[int] = []
    for x in ids or []:
        try:
            i = int(str(x).strip())
        except (TypeError, ValueError):
            continue
        if i not in out:
            out.append(i)
    return out


def load_config(library: Path) -> dict:
    """The library's dislike list. The first call after an update from LDTF 1.3 moves the per-archive copies here:
    the same list everywhere is kept as is; different lists — the most recently edited one wins."""
    path = config_path(library)
    neg = _read_negative(path)
    if neg is None:
        with _CONFIG_LOCK:
            neg = _read_negative(path)
            if neg is None:
                neg = _migrate(library)
    return {"_help": HELP, "negative": neg}


def _migrate(library: Path) -> list[int]:
    from .state import archive_dirs
    found: list[tuple[float, list[int], Path]] = []
    for d in archive_dirs(library):
        p = d / CONFIG_NAME
        neg = _read_negative(p)
        if neg is not None:
            found.append((p.stat().st_mtime, neg, p))
    if not found:
        neg = list(DEFAULT_NEGATIVE)
    elif all(sorted(f[1]) == sorted(found[0][1]) for f in found):
        neg = found[0][1]
    else:
        newest = max(found, key=lambda f: f[0])
        neg = newest[1]
        log.info(f"[реакции] в архивах были разные списки дизлайков — взят последний изменённый (@{newest[2].parent.name})")
    try:
        write_json(config_path(library), {"_help": HELP, "negative": neg})
    except OSError as e:   # a read-only library: the list still applies, the archives keep their files
        log.warning(f"[реакции] не удалось сохранить {config_path(library)}: {e}")
        return neg
    for _, _, p in found:
        try:
            p.unlink()
        except OSError:
            pass
    if found:
        log.info(f"[реакции] список дизлайков теперь общий для всех архивов: {config_path(library)}")
    return neg


def save_negative(library: Path, ids: Any) -> list[int]:
    neg = clean_ids(ids)
    with _CONFIG_LOCK:
        write_json(config_path(library), {"_help": HELP, "negative": neg})
    return neg


_CATALOG: dict[str, Any] = {"stamp": None, "assets": {}}


def library_assets(library: Path) -> dict:
    """{"reactions": [...]} of every archive of the library together: a reaction is retired only if every archive
    that knows it says so (cached until an archive saves a new catalog)."""
    from .state import archive_dirs
    files = [d / "raw" / "assets.json.gz" for d in archive_dirs(library)]
    stamp = tuple((str(f), f.stat().st_mtime) for f in files if f.exists())
    with _CONFIG_LOCK:
        if _CATALOG["stamp"] == stamp:
            return _CATALOG["assets"]
    merged: dict[str, dict] = {}
    for f, _ in stamp:
        try:
            data = read_json_gz(Path(f), {})
        except (OSError, ValueError):
            continue
        for x in data.get("reactions") or []:
            if not isinstance(x, dict) or x.get("id") is None:
                continue
            k = str(x["id"])
            cur = merged.get(k)
            if cur is None or (cur.get("retired") and not x.get("retired")):
                merged[k] = x
    assets = {"reactions": list(merged.values())}
    with _CONFIG_LOCK:
        _CATALOG.update(stamp=stamp, assets=assets)
    return assets


class Reactions:
    def __init__(self, assets: dict, resolver: MediaResolver, report: Any, config: dict | None = None):
        self.resolver = resolver
        self.report = report
        self.cat: dict[str, dict] = {str(x.get("id")): x for x in assets.get("reactions", []) if isinstance(x, dict)}
        self.negative = {str(i) for i in (config or {}).get("negative", DEFAULT_NEGATIVE)}

    @classmethod
    def for_library(cls, library: Path, resolver: MediaResolver, report: Any) -> "Reactions":
        """The reactions every archive of the library saved and the library's dislike list (reactions.config.json)."""
        return cls(library_assets(library), resolver, report, load_config(library))

    @classmethod
    def for_archive(cls, arch: Any, resolver: MediaResolver, report: Any) -> "Reactions":
        return cls.for_library(arch.library, resolver, report)

    def is_negative(self, rid: Any) -> bool:
        return str(rid) in self.negative

    def label(self, rid: Any) -> str | None:
        try:
            return LABELS.get(int(rid))
        except (TypeError, ValueError):
            return None

    def _url(self, key: str | None, R: str) -> str | None:
        if not key:
            return None
        loc = self.resolver.local(key)
        return R + loc["path"] if loc else media_url(key)

    def srcs(self, rid: Any, R: str) -> tuple[str | None, str | None]:
        """(static png, animated webp) — the animated original is fetched with -/format/raw/."""
        x = self.cat.get(str(rid))
        if not x:
            return None, None
        return self._url(x.get("staticUuid"), R), self._url(raw_key(x.get("animatedUuid")), R)

    def img(self, rid: Any, R: str, alt: str = "") -> str | None:
        st, an = self.srcs(rid, R)
        if not (st or an):
            return None
        rem = ' data-remote="reaction"' if (an or st or "").startswith("http") else ""   # not in the archive: from DTF
        if an and st:  # animated, but static for people who prefer reduced motion
            return (f'<picture><source srcset="{E(st)}" media="(prefers-reduced-motion: reduce)">'
                    f'<img src="{E(an)}" alt="{E(alt)}" loading="lazy"{rem}></picture>')
        return f'<img src="{E(an or st)}" alt="{E(alt)}" loading="lazy"{rem}>'

    def split(self, pairs: list[tuple[Any, int]]) -> tuple[int, int]:
        pos = sum(n for rid, n in pairs if not self.is_negative(rid))
        neg = sum(n for rid, n in pairs if self.is_negative(rid))
        return pos, neg

    def score_html(self, pairs: list[tuple[Any, int]], official: int | None = None) -> str:
        pos, neg = self.split(pairs)
        if not pos and not neg:
            return ""
        title = f"Позитивных реакций: {pos}, негативных: {neg}"
        if official is not None:
            title += f". Рейтинг DTF: {official} (сайт считает любую реакцию как +1)"
        down = f' <span class="neg">▼ {neg}</span>' if neg else ""
        return f'<span class="score" title="{E(title)}"><span class="pos">▲ {pos}</span>{down}</span>'

    def html(self, pairs: list[tuple[Any, int]], R: str, where: str, limit: int = 12) -> str:
        chips = []
        for rid, n in pairs[:limit]:
            img = self.img(rid, R, f"#{rid}")
            neg = " neg" if self.is_negative(rid) else ""
            name = self.label(rid)
            title = f"Реакция #{rid}" + (f" — {name}" if name else "") + (" (дизлайк)" if neg else "")
            if img:
                chips.append(f'<span class="rx{neg}" title="{E(title)}">{img}{n}</span>')
            else:
                if self.report is not None:
                    self.report.add("unknownReactions", str(rid), where)
                chips.append(f'<span class="rx unknown{neg}" title="Неизвестная реакция #{E(str(rid))} (нет в каталоге)">'
                             f'<span class="rx-q">?</span>{n}</span>')
        if len(pairs) > limit:
            chips.append(f'<span class="rx more">+{sum(n for _, n in pairs[limit:])}</span>')
        return f'<span class="rxs">{"".join(chips)}</span>' if chips else ""

    def norm(self, pairs: list[tuple[Any, int]]) -> dict:
        pos, neg = self.split(pairs)
        items = []
        for rid, n in pairs:
            it: dict[str, Any] = {"id": rid, "count": n, "polarity": "negative" if self.is_negative(rid) else "positive"}
            if self.label(rid):
                it["label"] = self.label(rid)
            if str(rid) not in self.cat:
                it["unknown"] = True
            items.append(it)
        return {"total": pos + neg, "positive": pos, "negative": neg, "items": items}

    def catalog(self) -> list[dict]:
        out = []
        for rid, x in sorted(self.cat.items(), key=lambda t: (len(t[0]), t[0])):
            row: dict[str, Any] = {"id": x.get("id"), "label": self.label(rid), "type": x.get("type"),
                                   "polarity": "negative" if self.is_negative(rid) else "positive",
                                   "retired": bool(x.get("retired"))}
            for field, name in (("staticUuid", "static"), ("animatedUuid", "animated")):
                key = x.get(field)
                if key:
                    loc = self.resolver.local(key)
                    if name == "animated":
                        loc = self.resolver.local(raw_key(key)) or loc
                    row[name] = {"uuid": key, "local": loc["path"] if loc else None}
            out.append(row)
        return out
