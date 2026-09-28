"""Reactions: catalog (raw/assets.json.gz), icons, polarity (likes vs dislikes) and fallbacks.

DTF itself counts every reaction as +1 in `likes.counterLikes` (there are no dislikes on the new
DTF). Some reactions are clearly negative, so the archive splits them into ▲ positive / ▼ negative
using an editable config: <archive>/reactions.config.json (created on first render).
Unknown reaction ids (not in the catalog) render as a "?" chip and are listed in the render report.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .media import raw_key
from .normalize import MediaResolver
from .util import write_json

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


def load_config(archive_root: Path) -> dict:
    path = archive_root / CONFIG_NAME
    cfg = {
        "_help": ("Какие реакции считать дизлайками (▼). Укажите id реакций; картинки и id — в data/reactions.json "
                  "и на странице site/reactions.html. После правки запустите: dtf-backup render"),
        "negative": DEFAULT_NEGATIVE,
    }
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user.get("negative"), list):
                cfg["negative"] = user["negative"]
        except (OSError, ValueError):
            pass
    else:
        write_json(path, cfg)
    return cfg


class Reactions:
    def __init__(self, assets: dict, resolver: MediaResolver, report: Any, config: dict | None = None):
        self.resolver = resolver
        self.report = report
        self.cat: dict[str, dict] = {str(x.get("id")): x for x in assets.get("reactions", []) if isinstance(x, dict)}
        self.negative = {str(i) for i in (config or {}).get("negative", DEFAULT_NEGATIVE)}

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
        return R + loc["path"] if loc else key if key.startswith("http") else f"https://leonardo.osnova.io/{key}/"

    def srcs(self, rid: Any, R: str) -> tuple[str | None, str | None]:
        """(static png, animated webp) — the animated original is fetched with -/format/raw/."""
        x = self.cat.get(str(rid))
        if not x:
            return None, None
        return self._url(x.get("staticUuid"), R), self._url(raw_key(x.get("animatedUuid")), R)

    def src(self, rid: Any, R: str) -> str | None:
        st, an = self.srcs(rid, R)
        return an or st

    def img(self, rid: Any, R: str, alt: str = "") -> str | None:
        st, an = self.srcs(rid, R)
        if not (st or an):
            return None
        if an and st:  # animated, but static for people who prefer reduced motion
            return (f'<picture><source srcset="{E(st)}" media="(prefers-reduced-motion: reduce)">'
                    f'<img src="{E(an)}" alt="{E(alt)}" loading="lazy"></picture>')
        return f'<img src="{E(an or st)}" alt="{E(alt)}" loading="lazy">'

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
