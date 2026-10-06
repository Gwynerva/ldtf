"""Versions of posts and comments (history.py): what a sync records when DTF edits, removes or returns something,
that live data never makes a version, that one change is recorded once, the import of LDTF 1.3's raw/history files,
and the pages that show it."""

import copy
import gzip
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup import history  # noqa: E402
from dtf_backup.state import Archive, unpack  # noqa: E402
from dtf_backup.sync import Syncer  # noqa: E402
from test_guard import LONG, SAMPLES, FakeDtf  # noqa: E402


class SigTest(unittest.TestCase):
    def test_live_data_is_no_change(self) -> None:
        p = {"id": 1, "title": "Пост", "blocks": [
            {"type": "text", "data": {"text": "<p>Текст тут</p>"}},
            {"type": "media", "data": {"items": [{"image": {"type": "image", "data": {"uuid": "u1", "width": 10,
                                                                                   "base64preview": "x", "size": 5}}}]}},
            {"type": "osnovaEmbed", "data": {"osnovaEmbed": {"original_id": 7, "title": "Т", "likes": 3}}}],
             "counters": {"comments": 1}, "reactions": {"counters": []}, "donations": {"amount": 1},
             "author": {"isOnline": True}}
        q = copy.deepcopy(p)
        q.update(counters={"comments": 9}, donations={"amount": 99}, author={"isOnline": False}, dateModified=5)
        q["blocks"][1]["data"]["items"][0]["image"]["data"].update(base64preview="y", size=6)
        q["blocks"][2]["data"]["osnovaEmbed"]["likes"] = 30
        q["blocks"][0]["data"]["text"] = "<p>Текст  \t тут</p>"   # whitespace only
        self.assertEqual(history.content_sig("post", p), history.content_sig("post", q))
        q["blocks"][0]["data"]["text"] = "<p>Текст, исправленный</p>"
        self.assertNotEqual(history.content_sig("post", p), history.content_sig("post", q))
        c = {"id": 3, "text": "Привет", "media": [], "likes": {"counterLikes": 1}, "isEdited": False}
        d = dict(c, likes={"counterLikes": 5}, isEdited=True, lastModificationDate=9, donation=100)
        self.assertFalse(history.edited("comment", c, d))
        self.assertTrue(history.edited("comment", c, dict(d, text="Привет!")))
        self.assertFalse(history.edited("comment", c, dict(d, text="Комментарий недоступен")))   # a placeholder

    def test_word_diff(self) -> None:
        h = history.word_diff("Кот спит на <окне>", "Кот спал на <окне> днём")
        self.assertIn("<del>спит</del><ins>спал</ins>", h)
        self.assertIn("<ins> днём</ins>", h)
        self.assertIn("&lt;окне&gt;", h)
        ops = history.block_ops([{"t": 1}, {"t": 2}, {"t": 3}], [{"t": 1}, {"t": 4}, {"t": 3}, {"t": 5}])
        self.assertEqual([o[0] for o in ops], ["equal", "replace", "equal", "insert"])


class HistorySyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lib = Path(self.tmp.name)
        self.fake = FakeDtf()
        self.assertEqual(self.sync(user="tester"), 0)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        self.tmp.cleanup()

    def sync(self, user=None, full=False) -> int:
        arch = Archive(self.lib / "tester", self.lib)
        try:
            s = Syncer(arch, user, workers=3, no_media=True, refresh_days=30, full=full)
            s.api = self.fake
            return s.run()
        finally:
            arch.close()

    def rows(self, kind=None, item=None) -> list[dict]:
        arch = Archive(self.lib / "tester", self.lib)
        try:
            q, args = "SELECT * FROM history WHERE 1", []
            if kind:
                q, args = q + " AND kind=?", args + [kind]
            if item:
                q, args = q + " AND item_id=?", args + [item]
            return [dict(r) for r in arch.db.execute(q + " ORDER BY id", args)]
        finally:
            arch.close()

    def test_first_sync_has_no_history(self) -> None:
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows(), [])

    def test_post_edit_and_same_content(self) -> None:
        p = self.fake.posts[2]
        old_blocks = copy.deepcopy(p["blocks"])
        p["dateModified"] += 10                     # touched on DTF, content the same: no version
        p["counters"] = {"comments": 5}
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows("post"), [])
        p["blocks"].append({"type": "text", "data": {"text": "<p>Дополнение</p>"}})
        p["dateModified"] += 10
        self.assertEqual(self.sync(), 0)
        r = self.rows("post", 2)
        self.assertEqual([x["event"] for x in r], ["edit"])
        self.assertEqual(unpack(r[0]["body"])["blocks"], old_blocks)
        self.assertEqual(self.sync(full=True), 0)  # everything fetched again: nothing new
        self.assertEqual(len(self.rows("post")), 1)

    def test_comment_edit_recorded_once(self) -> None:
        """The owner's comment under the own post lives in the feed and in the post's tree: one edit, one row."""
        mine = self.fake.trees[4][1]
        old = mine["text"]
        mine["text"] = old + " — дополнено"
        mine["isEdited"] = True
        self.fake.posts[4]["dateModified"] += 5
        self.assertEqual(self.sync(), 0)
        r = self.rows("comment", mine["id"])
        self.assertEqual([x["event"] for x in r], ["edit"])
        self.assertEqual(unpack(r[0]["body"])["text"], old)
        self.assertEqual(r[0]["entry_id"], 4)

    def test_other_comment_removed_and_back(self) -> None:
        guest = self.fake.trees[5][0]
        text = guest["text"]
        guest["text"] = "Комментарий недоступен"
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.sync(), 0)                 # seen again: no second event
        guest["text"] = text + " (вернул и поправил)"
        self.assertEqual(self.sync(), 0)
        r = self.rows("comment", guest["id"])
        self.assertEqual([(x["event"], x["state"]) for x in r], [("removed", "unavailable"), ("edit", None), ("restored", None)])
        self.assertEqual(unpack(r[1]["body"])["text"], text)

    def test_post_removed_restored_removed(self) -> None:
        stub = SAMPLES["timeline_item_removed"]
        original = copy.deepcopy(self.fake.posts[3])
        self.fake.posts[3].update(title=stub["title"], blocks=copy.deepcopy(stub["blocks"]), isRemovedByUserRequest=True)
        self.assertEqual(self.sync(), 0)
        self.fake.posts[3] = copy.deepcopy(original)
        self.fake.posts[3]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)
        self.fake.posts[3].update(title=stub["title"], blocks=copy.deepcopy(stub["blocks"]), isRemovedByUserRequest=True)
        self.assertEqual(self.sync(), 0)
        self.assertEqual([(x["event"], x["state"]) for x in self.rows("post", 3)],
                         [("removed", "removed"), ("restored", None), ("removed", "removed")])

    def test_timeline_copy_is_no_version(self) -> None:
        p = {"id": 9, "title": "Новый пост", "date": self.fake.posts[8]["date"] + 3600, "url": "https://dtf.ru/tester/9",
             "counters": {"comments": 0}, "repostId": None, "author": {"id": 500}, "subsite": {"id": 500},
             "blocks": [{"type": "text", "data": {"text": "<p>Короткая версия из ленты</p>"}}]}
        p["dateModified"] = p["date"]
        self.fake.posts[9] = p
        self.fake.content_404.add(9)
        self.assertEqual(self.sync(), 0)
        self.fake.content_404.discard(9)
        p["blocks"].append({"type": "text", "data": {"text": "<p>Полный текст</p>"}})
        p["dateModified"] += 60
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows("post", 9), [])

    def test_legacy_files_are_imported(self) -> None:
        arch = Archive(self.lib / "tester", self.lib)
        f = arch.raw / "history" / "posts" / "2" / "1700000000.json.gz"
        f.parent.mkdir(parents=True)
        old = dict(self.fake.posts[2], blocks=[{"type": "text", "data": {"text": "<p>Старый текст</p>"}}],
                   dateModified=1700000000)
        f.write_bytes(gzip.compress(json.dumps(old, ensure_ascii=False).encode()))
        arch.close()
        self.assertEqual(self.sync(), 0)
        r = self.rows("post", 2)
        self.assertEqual((r[0]["event"], r[0]["version_date"]), ("edit", 1700000000))
        self.assertFalse((self.lib / "tester" / "raw" / "history").exists())
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(self.rows("post", 2)), 1)

    def test_drop_comments_drops_their_history(self) -> None:
        from dtf_backup.scope import drop_comments
        guest = self.fake.trees[5][0]
        guest["text"] = "Комментарий недоступен"
        self.fake.posts[2]["blocks"].append({"type": "text", "data": {"text": "<p>Правка</p>"}})
        self.fake.posts[2]["dateModified"] += 10
        self.assertEqual(self.sync(), 0)
        self.assertEqual({x["kind"] for x in self.rows()}, {"post", "comment"})
        arch = Archive(self.lib / "tester", self.lib)
        drop_comments(arch)
        arch.close()
        self.assertEqual({x["kind"] for x in self.rows()}, {"post"})


class HistoryPagesTest(unittest.TestCase):
    """The pages of an archive with an edited post, an edited and a removed comment."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.lib = Path(cls.tmp.name)
        cls.fake = f = FakeDtf()
        sync = cls._sync
        sync("tester")
        f.posts[2]["blocks"] = [{"type": "text", "data": {"text": f"<p>{LONG} (2), переписанный заново</p>"}},
                                {"type": "header", "data": {"text": "Новый раздел", "style": "h2"}}]
        f.posts[2]["title"] = "Пост номер 2 (исправленный)"
        f.posts[2]["dateModified"] += 100
        mine = f.trees[4][1]
        cls.cid = mine["id"]
        mine["text"] += " — с поправкой"
        guest = f.trees[5][0]
        cls.gid = guest["id"]
        guest["text"] = "Комментарий удалён модератором"
        guest["isRemoved"] = guest["isRemovedByModerator"] = True
        f.posts[4]["dateModified"] += 1
        f.posts[5]["dateModified"] += 1
        sync()
        from dtf_backup.render import render
        arch = Archive(cls.lib / "tester", cls.lib)
        render(arch)
        arch.close()
        from dtf_backup.web.server import App, Handler
        cls.app = App(cls.lib, 0)
        Handler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def _sync(cls, user=None) -> None:
        arch = Archive(cls.lib / "tester", cls.lib)
        s = Syncer(arch, user, workers=3, no_media=True, refresh_days=30)
        s.api = cls.fake
        assert s.run() == 0
        arch.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.invalidate()
        import gc
        gc.collect()
        cls.tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=20) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_view_counts(self) -> None:
        import sqlite3
        db = sqlite3.connect(self.lib / "tester" / ".state" / "view.sqlite")
        try:
            self.assertEqual(db.execute("SELECT versions, hist FROM posts WHERE id=2").fetchone(), (2, 1))
            self.assertEqual(db.execute("SELECT hist FROM comments WHERE id=?", (self.cid,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history").fetchone()[0], 3)
        finally:
            db.close()

    def test_post_history_pages(self) -> None:
        code, body = self.get("/u/tester/p/2")
        self.assertIn("/u/tester/p/2/history", body)
        self.assertIn("2 версии", body)
        code, body = self.get("/u/tester/p/2/history")
        self.assertEqual(code, 200)
        self.assertIn("Текущая версия", body)
        hid = json.loads(body.split('data-versions="', 1)[1].split('"', 1)[0].replace("&quot;", '"'))[0]
        code, body = self.get(f"/u/tester/p/2/history/{hid}")
        self.assertEqual(code, 200)
        self.assertIn("<ins>", body)
        self.assertIn("<del>", body)
        self.assertIn("Новый раздел", body)
        code, body = self.get(f"/u/tester/p/2/history/{hid}?full=1")
        self.assertIn("Это прежняя версия", body)
        self.assertEqual(self.get("/u/tester/p/2/history/999999")[0], 404)

    def test_comment_history_and_changes(self) -> None:
        code, body = self.get(f"/u/tester/p/4")
        self.assertIn(f"/u/tester/c/{self.cid}/history", body)
        code, body = self.get(f"/u/tester/c/{self.cid}/history")
        self.assertEqual(code, 200)
        self.assertIn("<ins>", body)
        self.assertIn("с поправкой", body)
        code, body = self.get(f"/u/tester/c/{self.gid}/history")
        self.assertIn("удалён модератором", body)
        code, body = self.get("/u/tester/changes")
        self.assertEqual(code, 200)
        self.assertIn("Пост номер 2", body)
        code, body = self.get("/u/tester/changes?e=removed")
        self.assertIn("удалён модератором", body)
        self.assertNotIn("исправленный", body)
        code, body = self.get("/u/tester/")
        self.assertIn("/u/tester/changes", body)


if __name__ == "__main__":
    unittest.main()
