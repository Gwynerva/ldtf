"""Pure-Python stemmers: Russian (Snowball) and English (Porter), plus tokenization matching FTS5 unicode61.

Snowball Russian sometimes cuts nouns like a verb ("катана" -> "ката", but "катану" -> "катан"), so every
Russian word also gets a "nominal" stem that strips only noun/adjective endings ("катана" -> "катан").
Index and queries use both (`word_stems`), which makes all forms of such words meet.
"""

from __future__ import annotations

import re
from functools import lru_cache

TOKEN_RE = re.compile(r"[^\W_]+")          # same word boundaries as FTS5 unicode61 (underscore separates)
CYR_RE = re.compile(r"[а-я]")
LAT_RE = re.compile(r"[a-z]")


def norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(norm(text))


# ---------------------------------------------------------------------- Russian (Snowball)
_V = set("аеиоуыэюя")


def _desc(*items: str) -> tuple[str, ...]:
    return tuple(sorted(items, key=len, reverse=True))


_PG = [(s, True) for s in ("в", "вши", "вшись")] + [(s, False) for s in ("ив", "ивши", "ившись", "ыв", "ывши", "ывшись")]
_PG = sorted(_PG, key=lambda t: len(t[0]), reverse=True)
_ADJ = _desc("ее", "ие", "ые", "ое", "ими", "ыми", "ей", "ий", "ый", "ой", "ем", "им", "ым", "ом", "его", "ого", "ему",
             "ому", "их", "ых", "ую", "юю", "ая", "яя", "ою", "ею")
_PART = sorted([(s, True) for s in ("ем", "нн", "вш", "ющ", "щ")] + [(s, False) for s in ("ивш", "ывш", "ующ")],
               key=lambda t: len(t[0]), reverse=True)
_REFL = _desc("ся", "сь")
_VERB = sorted([(s, True) for s in ("ла", "на", "ете", "йте", "ли", "й", "л", "ем", "н", "ло", "но", "ет", "ют", "ны",
                                    "ть", "ешь", "нно")] +
               [(s, False) for s in ("ила", "ыла", "ена", "ейте", "уйте", "ите", "или", "ыли", "ей", "уй", "ил", "ыл",
                                     "им", "ым", "ен", "ило", "ыло", "ено", "ят", "ует", "уют", "ит", "ыт", "ены",
                                     "ить", "ыть", "ишь", "ую", "ю")], key=lambda t: len(t[0]), reverse=True)
_NOUN = _desc("а", "ев", "ов", "ие", "ье", "е", "иями", "ями", "ами", "еи", "ии", "и", "ией", "ей", "ой", "ий", "й",
              "иям", "ям", "ием", "ем", "ам", "ом", "о", "у", "ах", "иях", "ях", "ы", "ь", "ию", "ью", "ю", "ия", "ья", "я")
_NOMINAL = _desc(*set(_ADJ) | set(_NOUN))


def _strip(rv: str, endings: tuple[str, ...]) -> str | None:
    for e in endings:
        if rv.endswith(e):
            return rv[: -len(e)]
    return None


def _strip_aya(rv: str, endings: list[tuple[str, bool]]) -> str | None:
    """Endings flagged True must be preceded by а or я (which stays)."""
    for e, need in endings:
        if rv.endswith(e):
            rest = rv[: -len(e)]
            if not need or rest.endswith(("а", "я")):
                return rest
    return None


def _regions(w: str) -> tuple[int, int]:
    """(pV, p2): start of RV and of R2."""
    pv = next((i + 1 for i, ch in enumerate(w) if ch in _V), len(w))

    def after_vc(start: int) -> int:
        for i in range(start + 1, len(w)):
            if w[i] not in _V and w[i - 1] in _V:
                return i + 1
        return len(w)
    p1 = after_vc(0)
    p2 = after_vc(p1) if p1 < len(w) else len(w)
    return pv, p2


@lru_cache(maxsize=400_000)
def ru_stem(word: str) -> str:
    w = norm(word)
    if len(w) < 3:
        return w
    pv, p2 = _regions(w)
    head, rv = w[:pv], w[pv:]
    r = _strip_aya(rv, _PG)
    if r is None:
        r0 = _strip(rv, _REFL)
        rv2 = r0 if r0 is not None else rv
        a = _strip(rv2, _ADJ)
        if a is not None:
            p = _strip_aya(a, _PART)
            r = p if p is not None else a
        else:
            v = _strip_aya(rv2, _VERB)
            if v is not None:
                r = v
            else:
                n = _strip(rv2, _NOUN)
                r = n if n is not None else rv2
    rv = r
    if rv.endswith("и"):
        rv = rv[:-1]
    w2 = head + rv
    for d in ("ость", "ост"):
        if w2.endswith(d) and len(w2) - len(d) >= p2 and len(w2) - len(d) >= pv:
            w2 = w2[: -len(d)]
            break
    rv = w2[pv:]
    if rv.endswith(("ейше", "ейш")):
        rv = rv[: -(4 if rv.endswith("ейше") else 3)]
        if rv.endswith("нн"):
            rv = rv[:-1]
    elif rv.endswith("нн"):
        rv = rv[:-1]
    elif rv.endswith("ь"):
        rv = rv[:-1]
    return head + rv


@lru_cache(maxsize=400_000)
def ru_nominal(word: str) -> str:
    """Strip only noun/adjective endings (plus a trailing ь/и) inside RV."""
    w = norm(word)
    if len(w) < 4:
        return w
    pv, _ = _regions(w)
    head, rv = w[:pv], w[pv:]
    n = _strip(rv, _NOMINAL)
    if n is not None:
        rv = n
    # nouns in -ей/-ай (косплей, музей): Snowball gives "коспл" for "косплей" but "коспле" for "косплея";
    # dropping a trailing vowel of the nominal stem makes all forms meet
    if len(rv) >= 2 and rv[-1] in _V:
        rv = rv[:-1]
    return head + rv


# ---------------------------------------------------------------------- English (Porter, 1980)
def _cons(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return False
    if ch == "y":
        return i == 0 or not _cons(w, i - 1)
    return True


def _m(stem: str) -> int:
    """Number of VC sequences."""
    n, i, L = 0, 0, len(stem)
    while i < L and _cons(stem, i):
        i += 1
    while i < L:
        while i < L and not _cons(stem, i):
            i += 1
        if i >= L:
            break
        while i < L and _cons(stem, i):
            i += 1
        n += 1
    return n


def _has_vowel(stem: str) -> bool:
    return any(not _cons(stem, i) for i in range(len(stem)))


def _double_c(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _cons(w, len(w) - 1)


def _cvc(w: str) -> bool:
    return (len(w) >= 3 and _cons(w, len(w) - 1) and not _cons(w, len(w) - 2) and _cons(w, len(w) - 3)
            and w[-1] not in "wxy")


_S2 = (("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"), ("izer", "ize"), ("abli", "able"),
       ("alli", "al"), ("entli", "ent"), ("eli", "e"), ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"),
       ("ator", "ate"), ("alism", "al"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"), ("aliti", "al"),
       ("iviti", "ive"), ("biliti", "ble"))
_S3 = (("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"), ("ful", ""), ("ness", ""))
_S4 = ("al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement", "ment", "ent", "ou", "ism", "ate", "iti",
       "ous", "ive", "ize")


@lru_cache(maxsize=200_000)
def en_stem(word: str) -> str:
    w = word.lower()
    if len(w) <= 2:
        return w
    # 1a
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("ss"):
        pass
    elif w.endswith("s"):
        w = w[:-1]
    # 1b
    flag = False
    if w.endswith("eed"):
        if _m(w[:-3]) > 0:
            w = w[:-1]
    elif w.endswith("ed") and _has_vowel(w[:-2]):
        w, flag = w[:-2], True
    elif w.endswith("ing") and _has_vowel(w[:-3]):
        w, flag = w[:-3], True
    if flag:
        if w.endswith(("at", "bl", "iz")):
            w += "e"
        elif _double_c(w) and w[-1] not in "lsz":
            w = w[:-1]
        elif _m(w) == 1 and _cvc(w):
            w += "e"
    # 1c
    if w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"
    # 2, 3
    for table in (_S2, _S3):
        for suf, rep in table:
            if w.endswith(suf):
                if _m(w[: -len(suf)]) > 0:
                    w = w[: -len(suf)] + rep
                break
    # 4
    for suf in sorted(_S4, key=len, reverse=True):
        if w.endswith(suf):
            if _m(w[: -len(suf)]) > 1:
                w = w[: -len(suf)]
            break
    else:
        if w.endswith("ion") and len(w) > 3 and w[-4] in "st" and _m(w[:-3]) > 1:
            w = w[:-3]
    # 5
    if w.endswith("e"):
        s = w[:-1]
        if _m(s) > 1 or (_m(s) == 1 and not _cvc(s)):
            w = s
    if _m(w) > 1 and _double_c(w) and w.endswith("l"):
        w = w[:-1]
    return w


# ---------------------------------------------------------------------- dispatch
@lru_cache(maxsize=500_000)
def word_stems(word: str) -> tuple[str, ...]:
    """Stems to index/query for one normalized token (1 or 2 variants)."""
    w = norm(word)
    if CYR_RE.search(w) and not LAT_RE.search(w):
        a, b = ru_stem(w), ru_nominal(w)
        return (a,) if a == b else (a, b)
    if LAT_RE.search(w) and not CYR_RE.search(w) and w.isalpha():
        return (en_stem(w),)
    return (w,)


VERBISH = re.compile(r"(ть|ться|тся|тись|чь|чься|ешь|ишь|ешься|ет|ит|ют|ут|ят|ат|ете|ите|ем|им|л|ла|ло|ли|лся|"
                     r"лась|лось|лись|йте|йся|вший|вшая|вшее|вшие|ющий|ющая|ющее|ющие)$")


@lru_cache(maxsize=100_000)
def query_stems(word: str) -> tuple[str, ...]:
    """Stems to search for a query word. The index holds both variants of every word; the query uses the
    nominal stem, plus the full Snowball stem only for verb-looking words — otherwise "катана" would also
    find "кататься" (Snowball: "ката")."""
    st = word_stems(word)
    if len(st) == 1:
        return st
    return st if VERBISH.search(norm(word)) else (st[1],)


def stem_text(text: str) -> str:
    """Space-separated stems of all tokens (the stemmed FTS columns)."""
    out: list[str] = []
    for t in tokens(text):
        out.extend(word_stems(t))
    return " ".join(out)
