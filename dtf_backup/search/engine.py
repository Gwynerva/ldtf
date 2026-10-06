"""Search index (built into view.sqlite by viewdb.build_view) and query engine used by the app.

Index
  search_docs(id, kind, ref, entry, date, author, title, body)   original texts (for snippets)
  fts  FTS5, external content (view fts_content): raw columns title/body (ё→е) + stemmed title_s/body_s
       (stems are only indexed, not stored); kind/ref/entry/date are UNINDEXED
  vocab(id, term, df) + vocab_tri (FTS5 trigram over vocab) — dictionary of word forms for typo correction

Query language (all combinable)
  катана            all word forms (катану, катаной…) + words starting with it
  "точная фраза"    exact words in this order
  -слово  -"фраза"  exclude
  игра OR фильм     either (also `|`)
  косп*             prefix only
Typos ("каcплей", "косплэй"), wrong keyboard layout ("rjcgktq") and look-alike Latin letters inside Russian
words are fixed automatically when a word is unknown to the archive; the page says what was corrected and
offers the literal search (exact=1). A prefix (косп*) gets only the layout and look-alike fixes, never a guess.
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .stemmers import CYR_RE, LAT_RE, TOKEN_RE, norm, query_stems, stem_text, tokens, word_stems

SCHEMA = """
CREATE TABLE search_docs (id INTEGER PRIMARY KEY, kind TEXT, ref INTEGER, entry INTEGER, date INTEGER,
    author INTEGER, title TEXT, body TEXT);
CREATE INDEX search_docs_ref ON search_docs(kind, ref);
CREATE VIEW fts_content AS SELECT id, kind, ref, entry, date, title, body, '' AS title_s, '' AS body_s FROM search_docs;
CREATE VIRTUAL TABLE fts USING fts5(kind UNINDEXED, ref UNINDEXED, entry UNINDEXED, date UNINDEXED,
    title, body, title_s, body_s, content='fts_content', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2', prefix='3');
CREATE TABLE vocab (id INTEGER PRIMARY KEY, term TEXT UNIQUE, df INTEGER);
CREATE VIRTUAL TABLE vocab_tri USING fts5(term, content='vocab', content_rowid='id', tokenize='trigram');
"""
# bm25 weights by column: kind, ref, entry, date, title, body, title_s, body_s
WEIGHTS = (0, 0, 0, 0, 8.0, 3.0, 4.0, 1.5)
# the archive is about one person: their posts and comments get a boost over other people's comments
BM25 = ("bm25(fts, " + ", ".join(str(w) for w in WEIGHTS) + ") * "
        "CASE d.kind WHEN 'p' THEN 1.8 WHEN 'c' THEN 1.2 ELSE 1.0 END")

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от
меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом
себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж
тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех
никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три эту
моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между это
the a an of to in is are and or for on at by with it this that be as""".split())

EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
RU = "ёйцукенгшщзхъфывапролджэячсмитьбю."
EN2RU = str.maketrans(EN, RU)
RU2EN = str.maketrans(RU, EN)
LAT2CYR = str.maketrans("aceopxykmth", "асеорхукмтн")
CYR2LAT = str.maketrans("асеорхук", "aceopxyk")


# ---------------------------------------------------------------------- index
class IndexWriter:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript(SCHEMA)
        self.n = 0

    def add(self, kind: str, ref: int, entry: int | None, date: int, author: int | None, title: str, body: str) -> None:
        self.n += 1
        title, body = title or "", body or ""
        self.db.execute("INSERT INTO search_docs VALUES (?,?,?,?,?,?,?,?)",
                        (self.n, kind, ref, entry, date, author, title, body))
        self.db.execute("INSERT INTO fts(rowid, kind, ref, entry, date, title, body, title_s, body_s) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (self.n, kind, ref, entry, date, norm(title), norm(body), stem_text(title), stem_text(body)))

    def finish(self) -> None:
        db = self.db
        db.execute("CREATE VIRTUAL TABLE temp.fts_vocab USING fts5vocab(main, fts, col)")
        db.execute("INSERT INTO vocab(term, df) SELECT term, SUM(doc) FROM temp.fts_vocab "
                   "WHERE col IN ('title', 'body') AND length(term) BETWEEN 3 AND 40 "
                   "AND (term GLOB '*[a-z]*' OR term GLOB '*[а-я]*') GROUP BY term")
        db.execute("DROP TABLE temp.fts_vocab")
        db.execute("INSERT INTO vocab_tri(vocab_tri) VALUES ('rebuild')")
        db.execute("INSERT INTO fts(fts) VALUES ('optimize')")


def has_index(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='search_docs'").fetchone() is not None


# ---------------------------------------------------------------------- query model
@dataclass
class Item:
    words: list[str]               # normalized words (1 = term, >1 = phrase)
    raw: str                       # as typed
    phrase: bool = False
    prefix: bool = False
    neg: bool = False
    note: str | None = None        # correction applied ("каcплей → косплей")
    original: list[str] | None = None


@dataclass
class Query:
    clauses: list[list[Item]]      # AND of OR-groups
    negs: list[Item]
    text: str
    corrected: bool = False

    def positive_items(self) -> list[Item]:
        return [it for cl in self.clauses for it in cl]


QTOKEN = re.compile(r'(-?)"([^"]*)"?|(\|)|(-?)([^\s"|]+)')


def parse(text: str) -> Query:
    clauses: list[list[Item]] = []
    negs: list[Item] = []
    pending_or = False
    for m in QTOKEN.finditer(text or ""):
        neg_p, phrase, bar, neg_t, tok = m.groups()
        if bar or tok == "OR" or tok == "ИЛИ":
            pending_or = bool(clauses)
            continue
        if phrase is not None:
            words = tokens(phrase)
            if not words:
                continue
            it = Item(words, m.group(0), phrase=len(words) > 1, neg=bool(neg_p))
        else:
            prefix = tok.endswith("*")
            words = tokens(tok.rstrip("*"))
            if not words:
                continue
            it = Item(words, m.group(0), phrase=len(words) > 1, prefix=prefix and len(words) == 1, neg=bool(neg_t))
        if it.neg:
            negs.append(it)
            pending_or = False
            continue
        if pending_or and clauses:
            clauses[-1].append(it)
        else:
            clauses.append([it])
        pending_or = False
    return Query(clauses, negs, text)


# ---------------------------------------------------------------------- expressions
def q(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def item_expr(it: Item) -> str:
    if it.phrase:
        return "{title body} : " + q(" ".join(it.words))
    w = it.words[0]
    if it.prefix:
        return "{title body} : " + q(w) + "*"
    stems = query_stems(w)
    parts = ["{title_s body_s} : (" + " OR ".join(q(s) for s in stems) + ")"]
    parts.append("{title body} : " + q(w) + ("*" if len(w) >= 3 else ""))
    return "(" + " OR ".join(parts) + ")"


def build_match(query: Query) -> str | None:
    clauses = query.clauses
    real = [cl for cl in clauses if not (len(cl) == 1 and not cl[0].phrase and cl[0].words[0] in STOP)]
    use = real or clauses
    if not use:
        return None
    pos = " AND ".join("(" + " OR ".join(item_expr(it) for it in cl) + ")" for cl in use)
    if query.negs:
        pos = f"({pos}) NOT ({' OR '.join(item_expr(it) for it in query.negs)})"
    return pos


def near_expr(query: Query) -> str | None:
    """Documents where the query words stand close together (any word forms) are ranked first."""
    words = [it.words[0] for cl in query.clauses if len(cl) == 1 for it in cl if not it.phrase and not it.prefix]
    if len(words) < 2:
        return None
    stems = [query_stems(w)[-1] for w in words[:8]]
    return "{title_s body_s} : NEAR(" + " ".join(q(s) for s in stems) + f", {2 * len(stems)})"


# ---------------------------------------------------------------------- corrections
def damerau(a: str, b: str, limit: int = 3) -> int:
    """Optimal string alignment distance (transpositions count as 1); early exit above limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


class Corrector:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._known: dict[str, bool] = {}

    def df(self, w: str) -> int:
        r = self.db.execute("SELECT df FROM vocab WHERE term = ?", (w,)).fetchone()
        return r[0] if r else 0

    def known(self, w: str) -> bool:
        """The archive has this word in some form (stem or a word starting with it)."""
        if w not in self._known:
            it = Item([w], w)
            try:
                hit = self.db.execute("SELECT rowid FROM fts WHERE fts MATCH ? LIMIT 1", (item_expr(it),)).fetchone()
            except sqlite3.OperationalError:
                hit = None
            self._known[w] = hit is not None
        return self._known[w]

    def candidates(self, w: str, max_d: int) -> list[tuple[int, int, str]]:
        """(distance, -df, term) for dictionary words close to w."""
        if len(w) < 4:
            return []
        grams = {w[i:i + 3] for i in range(len(w) - 2)}
        expr = " OR ".join(q(g) for g in grams)
        try:
            rows = self.db.execute("SELECT v.term, v.df FROM vocab_tri t JOIN vocab v ON v.id = t.rowid "
                                   "WHERE vocab_tri MATCH ? ORDER BY rank LIMIT 400", (expr,)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for term, df in rows:
            if abs(len(term) - len(w)) > max_d or term == w:
                continue
            d = damerau(w, term, max_d)
            if d <= max_d:
                out.append((d, -df, term))
        out.sort()
        return out

    @staticmethod
    def max_dist(w: str) -> int:
        return 0 if len(w) < 4 else 1 if len(w) <= 7 else 2

    def fix(self, it: Item) -> None:
        """Correct an unknown single word in place (look-alike letters, keyboard layout, typos)."""
        if it.phrase or len(it.words) != 1:
            return
        w = it.words[0]
        if w.isdigit() or self.known(w):
            return
        tries: list[tuple[str, str]] = []
        if CYR_RE.search(w) and LAT_RE.search(w):
            fixed = w.translate(LAT2CYR) if len(CYR_RE.findall(w)) >= len(LAT_RE.findall(w)) else w.translate(CYR2LAT)
            tries.append((fixed, "латинские буквы в русском слове"))
        if LAT_RE.search(w) and not CYR_RE.search(w):
            tries.append((w.translate(EN2RU), "раскладка клавиатуры"))
        elif CYR_RE.search(w) and not LAT_RE.search(w):
            tries.append((w.translate(RU2EN), "раскладка клавиатуры"))
        base = w
        for cand, why in tries:
            cand_w = tokens(cand)
            if len(cand_w) != 1:
                continue
            if self.known(cand_w[0]):
                self._apply(it, cand_w[0], why)
                return
            if why.startswith("латинские"):
                base = cand_w[0]       # e.g. "каcплей" -> "касплей", still a typo: fuzzy below
        if it.prefix:                  # "пелемен*": the forms are chosen on purpose, no guessing ("перемен*")
            return
        best = self.candidates(base, self.max_dist(base))
        if best:
            self._apply(it, best[0][2], "опечатка")

    def _apply(self, it: Item, w: str, why: str) -> None:
        it.original = list(it.words)
        it.words = [w]
        it.note = why

    def suggest(self, query: Query) -> str | None:
        """'Возможно, вы искали': replace rare words by much more frequent close ones."""
        changed = False
        parts = []
        for m in QTOKEN.finditer(query.text):
            tok = m.group(0)
            if m.group(5) and not m.group(4) and not tok.endswith("*") and tok not in ("OR", "ИЛИ"):
                ws = tokens(tok)
                if len(ws) == 1 and len(ws[0]) >= 4 and not ws[0].isdigit():
                    w = ws[0]
                    mine = self.df(w)
                    best = self.candidates(w, self.max_dist(w))
                    if best and -best[0][1] >= max(20, 10 * mine):
                        parts.append(best[0][2])
                        changed = True
                        continue
            parts.append(tok)
        return " ".join(parts) if changed else None


# ---------------------------------------------------------------------- highlighting
class Marker:
    """Marks every form of the query words in a text (stem, prefix or exact phrase words)."""

    def __init__(self, query: Query):
        self.stems: set[str] = set()
        self.prefixes: list[str] = []
        self.exact: set[str] = set()
        for it in query.positive_items():
            if it.phrase:
                self.exact.update(it.words)
            elif it.prefix:
                self.prefixes.append(it.words[0])
            else:
                w = it.words[0]
                self.stems.update(query_stems(w))
                if len(w) >= 3:
                    self.prefixes.append(w)
                else:
                    self.exact.add(w)

    def hit(self, token: str) -> bool:
        t = norm(token)
        if t in self.exact:
            return True
        if self.stems and any(s in self.stems for s in word_stems(t)):
            return True
        return any(t.startswith(p) for p in self.prefixes)

    def snippet(self, text: str, width: int = 260) -> tuple[str, int]:
        """(HTML snippet around the densest cluster of hits, number of hits)."""
        text = text or ""
        spans = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text) if self.hit(m.group(0))]
        if not spans:
            cut = text[:width]
            return html.escape(cut) + ("…" if len(text) > width else ""), 0
        best_i, best_n = 0, 0
        j = 0
        for i, (s, _) in enumerate(spans):
            while spans[j][0] < s - width // 2 and j < i:
                j += 1
            if i - j + 1 > best_n:
                best_n, best_i = i - j + 1, j
        center = spans[best_i][0]
        start = max(0, center - width // 4)
        if start > 0:
            sp = text.rfind(" ", 0, start)
            start = sp + 1 if sp >= start - 20 else start
        end = min(len(text), start + width)
        if end < len(text):
            sp = text.find(" ", end)
            end = sp if 0 < sp < end + 20 else end
        out, pos = [], start
        for s, e in spans:
            if e <= start or s >= end:
                continue
            out.append(html.escape(text[pos:s]))
            out.append("<mark>" + html.escape(text[s:e]) + "</mark>")
            pos = e
        out.append(html.escape(text[pos:end]))
        return ("…" if start > 0 else "") + "".join(out).replace("\n", " ") + ("…" if end < len(text) else ""), len(spans)


# ---------------------------------------------------------------------- search
@dataclass
class Hit:
    rowid: int
    kind: str
    ref: int
    entry: int | None
    date: int
    author: int | None
    title: str
    body: str
    score: float
    close: bool


@dataclass
class Result:
    query: Query
    total: int = 0
    hits: list[Hit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suggestion: str | None = None
    error: str | None = None
    seconds: float = 0.0
    marker: Marker | None = None


@dataclass
class Match:
    """The hits of a query as SQL: `FROM {Match.FROM} WHERE {where}` with `args` (search_docs is `d`). search() pages
    through them; the MCP tool `count` groups them. `where` is empty when there is nothing to match (see res.error)."""
    res: Result
    where: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    corr: Corrector | None = None

    FROM = "fts JOIN search_docs d ON d.id = fts.rowid"


def prepare(db: sqlite3.Connection, text: str, kind: str = "", date_from: int | None = None,
            date_to: int | None = None, exact: bool = False) -> Match:
    query = parse(text)
    res = Result(query)
    if not query.clauses:
        res.error = "нужно хотя бы одно слово без минуса" if query.negs else None
        return Match(res)
    corr = Corrector(db)
    if not exact:
        for it in query.positive_items():
            corr.fix(it)
            if it.note:
                query.corrected = True
                res.notes.append(f"«{' '.join(it.original or [])}» → «{it.words[0]}» ({it.note})")
    match = build_match(query)
    if not match:
        return Match(res, corr=corr)
    where = ["fts MATCH :m"]
    args: dict[str, Any] = {"m": match}
    if kind in ("p", "c", "o"):
        where.append("d.kind = :k")
        args["k"] = kind
    if date_from is not None:
        where.append("d.date >= :a")
        args["a"] = date_from
    if date_to is not None:
        where.append("d.date < :b")
        args["b"] = date_to
    return Match(res, " AND ".join(where), args, corr)


def search(db: sqlite3.Connection, text: str, kind: str = "", date_from: int | None = None, date_to: int | None = None,
           sort: str = "rank", page: int = 1, per: int = 50, exact: bool = False) -> Result:
    t0 = time.perf_counter()
    m = prepare(db, text, kind, date_from, date_to, exact)
    res, query = m.res, m.res.query
    if not m.where:
        return res
    args = dict(m.args)
    near = near_expr(query)
    close_sql = "0"
    if near and sort == "rank":
        close_sql = "(fts.rowid IN (SELECT rowid FROM fts WHERE fts MATCH :near))"
        args["near"] = near
    order = {"date": "d.date DESC", "old": "d.date ASC"}.get(sort, "close DESC, score")
    try:
        res.total = db.execute(f"SELECT COUNT(*) FROM {Match.FROM} WHERE {m.where}", m.args).fetchone()[0]
        rows = db.execute(
            f"SELECT fts.rowid, d.kind, d.ref, d.entry, d.date, d.author, d.title, d.body, {BM25} "
            f"AS score, {close_sql} AS close FROM {Match.FROM} WHERE {m.where} "
            f"ORDER BY {order} LIMIT :lim OFFSET :off", dict(args, lim=per, off=(max(page, 1) - 1) * per)).fetchall()
    except sqlite3.OperationalError as e:
        res.error = f"не удалось разобрать запрос: {e}"
        return res
    res.hits = [Hit(*r[:8], score=r[8], close=bool(r[9])) for r in rows]
    if res.total < 5 and not exact and m.corr:
        res.suggestion = m.corr.suggest(query)
    res.marker = Marker(query)
    res.seconds = time.perf_counter() - t0
    return res


def other_forms(db: sqlite3.Connection, query: Query, limit: int = 6) -> list[tuple[str, int]]:
    """(term, df) of archive words that begin like a plain query word but the query does not find: diminutives and
    other forms with a stem of their own ("пельмени" finds "пельменей", not "пельмешки"). Words with short stems
    are skipped: "катана" would bring "катаклизм"."""
    marker = Marker(query)
    found: dict[str, int] = {}
    for it in query.positive_items():
        w = it.words[0]
        if it.phrase or it.prefix or it.note or w in STOP or LAT_RE.search(w) or not CYR_RE.search(w):
            continue
        stem = query_stems(w)[-1]
        if len(stem) < 6:
            continue
        lo = stem[:-1]
        hi = lo[:-1] + chr(ord(lo[-1]) + 1)
        for term, df in db.execute("SELECT term, df FROM vocab WHERE term >= ? AND term < ? ORDER BY df DESC LIMIT 200",
                                   (lo, hi)):
            if len(term) > len(lo) and not marker.hit(term):     # the bare cut stem is mostly another word
                found[term] = max(found.get(term, 0), df)
    return sorted(found.items(), key=lambda x: (-x[1], x[0]))[:limit]
