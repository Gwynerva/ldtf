"""Offline unit tests of the search building blocks: stemmers, query parsing, corrections, highlighting,
and the engine over a small in-memory index."""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.search.engine import (EN2RU, IndexWriter, Marker, build_match, damerau, other_forms, parse,  # noqa: E402
                                     prepare, search)
from dtf_backup.search.stemmers import en_stem, query_stems, stem_text, tokens, word_stems  # noqa: E402


class StemmerTest(unittest.TestCase):
    PARADIGMS = [
        ["катана", "катану", "катаной", "катаны", "катанами"],
        ["косплей", "косплея", "косплею", "косплеем", "косплее", "косплеи", "косплеев"],
        ["музей", "музея", "музеем", "музеев"],
        ["игра", "игры", "игре", "игрой", "играми"],
        ["комментарий", "комментария", "комментарии", "комментариями"],
        ["красивый", "красивая", "красивое", "красивыми"],
        ["ёлка", "елки", "ёлкой"],
    ]

    def test_forms_meet(self) -> None:
        for forms in self.PARADIGMS:
            q = set(query_stems(forms[0]))
            for f in forms:
                self.assertTrue(q & set(word_stems(f)), f"{forms[0]} vs {f}: {q} / {word_stems(f)}")

    def test_query_stems_precision(self) -> None:
        # Snowball treats "катана" like a verb ("ката"), which would also find "кататься"
        self.assertNotIn("ката", query_stems("катана"))
        self.assertFalse(set(query_stems("катана")) & set(word_stems("кататься")))
        # verbs keep the full stem, so other tenses match
        self.assertTrue(set(query_stems("сделать")) & set(word_stems("сделали")))

    def test_english(self) -> None:
        self.assertEqual(en_stem("running"), "run")
        self.assertEqual(en_stem("games"), en_stem("game"))
        self.assertEqual(en_stem("connection"), en_stem("connected"))

    def test_tokens_like_fts(self) -> None:
        self.assertEqual(tokens("snake_case Ёлка мир2077 don't"), ["snake", "case", "елка", "мир2077", "don", "t"])
        self.assertEqual(stem_text("Косплей Кейт"), "коспл кейт")


class QueryTest(unittest.TestCase):
    def test_parse(self) -> None:
        q = parse('косплей "life is strange" -игра игра2 OR фильм косп*')
        self.assertEqual([[i.words for i in cl] for cl in q.clauses],
                         [[["косплей"]], [["life", "is", "strange"]], [["игра2"], ["фильм"]], [["косп"]]])
        self.assertTrue(q.clauses[1][0].phrase)
        self.assertTrue(q.clauses[3][0].prefix)
        self.assertEqual([n.words for n in q.negs], [["игра"]])
        m = build_match(q)
        self.assertIn("NOT", m)
        self.assertIn('{title body} : "life is strange"', m)

    def test_stopwords_not_required(self) -> None:
        m = build_match(parse("как сделать бота"))
        self.assertNotIn('"как"', m)
        self.assertIn('"и"', build_match(parse("и")))   # a query of stopwords only still works

    def test_damerau_and_layout(self) -> None:
        self.assertEqual(damerau("косплей", "касплей"), 1)
        self.assertEqual(damerau("косплей", "кослпей"), 1)      # transposition
        self.assertGreater(damerau("косплей", "катана", 2), 2)
        self.assertEqual("rjcgktq".translate(EN2RU), "косплей")

    def test_marker(self) -> None:
        mk = Marker(parse("катана"))
        html, n = mk.snippet("Он рубил катаной, а потом продал КАТАНУ.")
        self.assertEqual(n, 2)
        self.assertIn("<mark>катаной</mark>", html)
        self.assertIn("<mark>КАТАНУ</mark>", html)
        html, n = Marker(parse("кейт")).snippet("<script>кейт</script>")
        self.assertIn("&lt;script&gt;", html)


class IndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        w = IndexWriter(self.db)
        for i, text in enumerate(["Сварил пельмени", "Пельмени со сметаной", "Пельмешки на ужин", "Острая катана"], 1):
            w.add("c", i, 100, 1790000000 + i, 7, "", text)
        w.finish()

    def tearDown(self) -> None:
        self.db.close()

    def test_prefix_not_guessed(self) -> None:
        res = search(self.db, "пелемен*")             # one letter off "пельмен*": taken as typed, not "fixed"
        self.assertEqual((res.total, res.notes), (0, []))
        res = search(self.db, "rfnfyf*")              # a keyboard layout slip is still fixed: it is no guess
        self.assertEqual(res.total, 1)
        self.assertIn("раскладка", res.notes[0])
        self.assertEqual(search(self.db, "пелемени").total, 2)                 # plain words: typos fixed as before

    def test_prepare(self) -> None:
        m = prepare(self.db, "пельмени", "c")
        self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {m.FROM} WHERE {m.where}", m.args).fetchone()[0], 2)
        self.assertEqual(prepare(self.db, "-пельмени").where, "")
        self.assertTrue(prepare(self.db, "-пельмени").res.error)

    def test_other_forms(self) -> None:
        self.assertEqual(other_forms(self.db, parse("пельмени")), [("пельмешки", 1)])
        self.assertEqual(other_forms(self.db, parse("пельмени OR пельмешки")), [])
        self.assertEqual(other_forms(self.db, parse("пельм*")), [])                # a prefix chooses its forms itself
        self.assertEqual(other_forms(self.db, parse("катана")), [])                # short stems: too much noise


if __name__ == "__main__":
    unittest.main()
