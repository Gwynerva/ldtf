"""Integrity: damaged, missing and stale files are found and repaired; a version DTF has now is never passed off as the
one the archive had (offline, a fake DTF)."""

import copy
import gzip
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup import integrity  # noqa: E402
from dtf_backup.history import content_sig  # noqa: E402
from dtf_backup.render import render  # noqa: E402
from dtf_backup.state import Archive, unpack  # noqa: E402
from dtf_backup.sync import Syncer  # noqa: E402
from dtf_backup.util import gz_json, read_json_gz, write_durable  # noqa: E402

from test_guard import FakeDtf  # noqa: E402


def damage(p: Path) -> None:
    """Garble the middle of a file (what a power loss or a broken disk leaves)."""
    b = bytearray(p.read_bytes())
    for i in range(len(b) // 3, len(b) // 3 + 16):
        b[i] ^= 0xFF
    p.write_bytes(bytes(b))


class IntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lib = Path(self.tmp.name)
        self.fake = FakeDtf()
        self.assertEqual(self.sync(user="tester"), 0)
        self.root = self.lib / "tester"

    def tearDown(self) -> None:
        import gc
        gc.collect()
        self.tmp.cleanup()

    def arch(self) -> Archive:
        return Archive(self.lib / "tester", self.lib)

    def sync(self, user=None, **kw) -> int:
        arch = self.arch()
        try:
            s = Syncer(arch, user, workers=3, no_media=True, refresh_days=30, **kw)
            s.api = self.fake
            self.last = s
            return s.run()
        finally:
            arch.close()

    def q(self, sql: str, *args):
        a = self.arch()
        try:
            return [dict(r) for r in a.db.execute(sql, args)]
        finally:
            a.close()

    def history(self, kind: str, item: int) -> list[tuple[str, str | None]]:
        return [(r["event"], r["state"]) for r in
                self.q("SELECT event, state FROM history WHERE kind=? AND item_id=? ORDER BY at, id", kind, item)]

    def log(self) -> list[dict]:
        a = self.arch()
        try:
            return a.get_meta(integrity.META_LOG) or []
        finally:
            a.close()

    # ------------------------------------------------------------------ the manifest
    def test_clean_sync_records_everything(self) -> None:
        rows = {r["path"]: r for r in self.q("SELECT * FROM files")}
        disk = {p.relative_to(self.root / "raw").as_posix() for p in (self.root / "raw").rglob("*.gz")}
        self.assertEqual(set(rows), disk)
        for path, r in rows.items():
            data = (self.root / "raw" / path).read_bytes()
            self.assertEqual(r["sha256"], hashlib.sha256(data).hexdigest(), path)
            self.assertEqual(r["backup_sha"], r["sha256"], path)    # the mirror has it
            self.assertEqual((self.root / ".state" / "backup" / "raw" / path).read_bytes(), data)
        sigs = self.q("SELECT id, content_sig FROM posts")
        self.assertTrue(all(r["content_sig"] for r in sigs))
        self.assertTrue(all(r["sig"] for r in self.q("SELECT sig FROM my_comments")))
        calls = self.fake.calls
        self.assertEqual(self.sync(), 0)                   # nothing damaged: nothing repaired, no extra requests
        self.assertEqual(self.last.check.found, [])
        self.assertLessEqual(self.fake.calls - calls, calls)

    # ------------------------------------------------------------------ posts
    def test_damaged_post_restored_from_the_mirror(self) -> None:
        p = self.root / "raw" / "posts" / "3.json.gz"
        good = p.read_bytes()
        damage(p)
        self.assertEqual(self.sync(), 0)
        self.assertEqual(p.read_bytes(), good)                       # exactly the bytes it had
        self.assertEqual(self.history("post", 3), [])                # nothing to tell in the history
        self.assertEqual(self.last.check.found[0]["outcome"], "restored")
        q = list((self.root / ".state" / "quarantine").rglob("3.json.gz"))
        self.assertEqual(len(q), 1)                                  # the damaged file is kept aside

    def test_damaged_post_without_copies_unchanged_on_dtf(self) -> None:
        self.drop_copies("posts/3.json.gz")
        damage(self.root / "raw" / "posts" / "3.json.gz")
        self.assertEqual(self.sync(), 0)
        self.assertEqual(read_json_gz(self.root / "raw" / "posts" / "3.json.gz")["title"], "Пост номер 3")
        self.assertEqual(self.history("post", 3), [])
        self.assertEqual(self.log()[0]["outcome"], "exact")
        self.assertEqual(self.q("SELECT content_status FROM posts WHERE id=3")[0]["content_status"], "ok")
        self.assertEqual(self.q("SELECT * FROM repairs"), [])

    def test_damaged_post_edited_on_dtf_is_marked(self) -> None:
        """The trap: DTF has another version now — it becomes current, the history says the original was lost."""
        self.drop_copies("posts/3.json.gz")
        a = self.arch()
        expect = a.db.execute("SELECT content_sig FROM posts WHERE id=3").fetchone()[0]
        a.close()
        damage(self.root / "raw" / "posts" / "3.json.gz")
        self.fake.posts[3]["blocks"].append({"type": "text", "data": {"text": "<p>Дописано потом</p>"}})
        self.fake.posts[3]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.history("post", 3), [("damaged", "replaced")])
        row = self.q("SELECT sig FROM history WHERE kind='post' AND item_id=3")[0]
        self.assertEqual(row["sig"], expect)                         # the signature of the lost version
        cur = read_json_gz(self.root / "raw" / "posts" / "3.json.gz")
        self.assertEqual(len(cur["blocks"]), 2)
        self.assertEqual(self.log()[0]["outcome"], "replaced")

    def test_older_copy_survives_as_a_version(self) -> None:
        """Only the pages' copy of an older version survived: it stays as a version, the lost one is marked."""
        self.drop_copies("posts/3.json.gz", view=False)
        render(self.arch())                                          # the pages have V1
        self.fake.posts[3]["title"] = "Пост номер 3 (V2)"
        self.fake.posts[3]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)                             # V2 archived, V1 an edit
        self.drop_copies("posts/3.json.gz", view=False)
        damage(self.root / "raw" / "posts" / "3.json.gz")            # V2 lost; the pages still have V1
        self.fake.posts[3]["title"] = "Пост номер 3 (V3)"
        self.fake.posts[3]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)
        h = self.history("post", 3)
        self.assertEqual(h.count(("edit", None)), 1)                 # V1 once (the same version is not kept twice)
        self.assertEqual(h[-1], ("damaged", "replaced"))
        self.assertEqual(read_json_gz(self.root / "raw" / "posts" / "3.json.gz")["title"], "Пост номер 3 (V3)")

    def test_damaged_post_deleted_on_dtf_is_lost(self) -> None:
        self.drop_copies("posts/3.json.gz")
        damage(self.root / "raw" / "posts" / "3.json.gz")
        self.fake.content_404.add(3)
        self.fake.hidden.add(3)
        self.assertEqual(self.sync(), 0)
        self.assertIn(("damaged", "lost"), self.history("post", 3))

    def test_missing_post_file(self) -> None:
        (self.root / "raw" / "posts" / "5.json.gz").unlink()
        self.assertEqual(self.sync(), 0)
        self.assertTrue((self.root / "raw" / "posts" / "5.json.gz").exists())
        self.assertEqual(self.last.check.found[0]["problem"], "missing")

    def test_newer_unrecorded_write_is_adopted(self) -> None:
        """A crash after a file was written but before its manifest row was committed: the file is newer, it stays."""
        p = self.root / "raw" / "posts" / "4.json.gz"
        obj = read_json_gz(p)
        obj["title"] = "Новее"
        obj["dateModified"] += 50
        write_durable(p, gz_json(obj))
        self.assertEqual(self.sync(), 0)
        self.assertEqual(read_json_gz(p)["title"], "Новее")             # DTF's listing has no newer date: kept
        self.assertEqual([f["outcome"] for f in self.last.check.found], ["adopted"])

    def test_stale_copy_put_back_is_caught(self) -> None:
        """Another program puts an old valid copy over the archive's file: caught, the version it had comes back."""
        p = self.root / "raw" / "posts" / "4.json.gz"
        old = p.read_bytes()
        self.fake.posts[4]["title"] = "Пост номер 4 (V2)"
        self.fake.posts[4]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)
        v2 = p.read_bytes()
        p.write_bytes(old)                                           # rolled back to V1
        self.assertEqual(self.sync(), 0)
        self.assertEqual(p.read_bytes(), v2)
        self.assertEqual(self.last.check.found[0]["problem"], "stale")

    # ------------------------------------------------------------------ comments
    def test_damaged_tree_keeps_kept_comments(self) -> None:
        guest = self.fake.trees[5][0]
        text = guest["text"]
        guest["text"] = "Комментарий недоступен"
        self.assertEqual(self.sync(), 0)                             # the guest's text is kept with a _site mark
        before = self.history("comment", guest["id"])
        damage(self.root / "raw" / "post-trees" / "5.json.gz")
        self.assertEqual(self.sync(), 0)
        items = {c["id"]: c for c in read_json_gz(self.root / "raw" / "post-trees" / "5.json.gz")["items"]}
        self.assertEqual(items[guest["id"]]["text"], text)
        self.assertEqual(self.history("comment", guest["id"]), before)   # no second "removed"

    def test_damaged_tree_without_mirror_is_reloaded(self) -> None:
        render(self.arch())
        self.drop_copies("post-trees/6.json.gz", view=False)
        damage(self.root / "raw" / "post-trees" / "6.json.gz")
        self.fake.trees[6][0]["text"] += " (поправлено)"
        self.assertEqual(self.sync(), 0)
        items = read_json_gz(self.root / "raw" / "post-trees" / "6.json.gz")["items"]
        self.assertTrue(items[0]["text"].endswith("(поправлено)"))
        cid = self.fake.trees[6][0]["id"]
        self.assertEqual(self.history("comment", cid), [("edit", None), ("damaged", "replaced")])
        self.assertEqual(self.q("SELECT tree_status FROM posts WHERE id=6")[0]["tree_status"], "ok")

    def test_damaged_thread_from_the_pages(self) -> None:
        render(self.arch())
        self.drop_copies("threads/105.json.gz", view=False)
        damage(self.root / "raw" / "threads" / "105.json.gz")
        self.assertEqual(self.sync(), 0)
        th = read_json_gz(self.root / "raw" / "threads" / "105.json.gz")
        self.assertEqual(len(th["items"]), 2)
        self.assertEqual(self.q("SELECT status FROM threads WHERE entry_id=105")[0]["status"], "ok")

    def test_damaged_own_comment_from_the_export(self) -> None:
        cid = self.fake.feed[0]["id"]
        a = self.arch()
        a.db.execute("UPDATE my_comments SET raw=? WHERE id=?", (b"\x00garbage", cid))
        a.commit()
        a.close()
        self.assertEqual(self.sync(), 0)
        c = unpack(self.q("SELECT raw FROM my_comments WHERE id=?", cid)[0]["raw"])
        self.assertEqual(c["text"], self.fake.feed[0]["text"])
        self.assertEqual(self.last.check.found[0]["outcome"], "restored")

    def test_damaged_own_comment_without_copies(self) -> None:
        mine = self.fake.feed[-1]                                    # the oldest: the head scan won't see it
        cid = mine["id"]
        year = None
        a = self.arch()
        a.db.execute("UPDATE my_comments SET raw=? WHERE id=?", (b"\x00garbage", cid))
        a.commit()
        a.close()
        for p in (self.root / "raw" / "my-comments").glob("*.jsonl.gz"):
            year = p.name
            p.write_bytes(b"broken")
        for p in (self.root / ".state" / "backup" / "raw" / "my-comments").glob("*"):
            p.unlink()
        mine["text"] = mine["text"] + " — поправлено"
        for items in self.fake.branches.values():
            for c in items:
                if c["id"] == cid:
                    c["text"] = mine["text"]
        self.assertEqual(self.sync(), 0)
        c = unpack(self.q("SELECT raw FROM my_comments WHERE id=?", cid)[0]["raw"])
        self.assertTrue(c["text"].endswith("поправлено"))
        self.assertIn(("damaged", "replaced"), self.history("comment", cid))
        self.assertIsNotNone(year)
        rows = [r["id"] for r in (read_json_gz_lines(self.root / "raw" / "my-comments" / year))]
        self.assertIn(cid, rows)                                     # the export is whole again

    def test_damaged_history_body_from_the_pages(self) -> None:
        p = self.fake.posts[2]
        p["blocks"].append({"type": "text", "data": {"text": "<p>Дополнение</p>"}})
        p["dateModified"] += 10
        self.assertEqual(self.sync(), 0)
        render(self.arch())
        hid = self.q("SELECT id FROM history WHERE kind='post' AND item_id=2")[0]["id"]
        a = self.arch()
        a.db.execute("UPDATE history SET body=? WHERE id=?", (b"\x01\x02", hid))
        a.commit()
        a.close()
        self.assertEqual(self.sync(), 0)
        body = unpack(self.q("SELECT body FROM history WHERE id=?", hid)[0]["body"])
        self.assertEqual(len(body["blocks"]), 1)

    # ------------------------------------------------------------------ the pages
    def test_render_never_builds_from_damaged_files(self) -> None:
        render(self.arch())
        self.drop_copies("posts/7.json.gz", view=False)
        damage(self.root / "raw" / "posts" / "7.json.gz")
        a = self.arch()
        try:
            render(a)        # repairs locally first (the pages' copy), then builds
        finally:
            a.close()
        self.assertEqual(read_json_gz(self.root / "raw" / "posts" / "7.json.gz")["title"], "Пост номер 7")

    # ------------------------------------------------------------------ media
    def test_media_slice_and_repair(self) -> None:
        a = self.arch()
        try:
            store = self.lib / "media"
            files = []
            for i in range(10):
                data = bytes([i]) * (100 + i)
                sha = hashlib.sha256(data).hexdigest()
                rel = f"{sha[:2]}/{sha}.jpg"
                (store / rel).parent.mkdir(parents=True, exist_ok=True)
                (store / rel).write_bytes(data)
                a.db.execute("INSERT INTO store.blob(sha256,size,ext,mime,path) VALUES (?,?,?,?,?)",
                             (sha, len(data), "jpg", "image/jpeg", rel))
                a.db.execute("INSERT INTO store.media_ref(key,status,sha256) VALUES (?,?,?)", (f"k{i}", "done", sha))
                a.db.execute("INSERT INTO main.media_use(key,owner) VALUES (?,?)", (f"k{i}", "post:1"))
                files.append((sha, store / rel))
            a.commit()
            integrity.MEDIA_MIN, old_min = 300, integrity.MEDIA_MIN
            try:
                c1 = integrity.Check(a, media=True).run()
                c2 = integrity.Check(a, media=True).run()
            finally:
                integrity.MEDIA_MIN = old_min
            self.assertGreater(c1.counts["media_hashed"], 0)
            self.assertLess(c1.counts["media_hashed"], 10)            # a slice, not everything
            first = {r[0] for r in a.db.execute("SELECT sha256 FROM store.blob WHERE checked_at IS NOT NULL")}
            self.assertGreater(len(first), c1.counts["media_hashed"])   # the second check took other files
            self.assertGreater(c2.counts["media_hashed"], 0)
            sha, path = files[3]
            path.write_bytes(b"X" * path.stat().st_size)              # same size, other bytes
            full = integrity.Check(a, mode="full", media=True).run()
            self.assertEqual([(f["kind"], f["problem"]) for f in full.found], [("media", "damaged")])
            self.assertFalse(path.exists())
            ref = a.db.execute("SELECT status, sha256, expect_sha FROM store.media_ref WHERE key='k3'").fetchone()
            self.assertEqual(tuple(ref), ("pending", None, sha))
            self.assertIsNone(a.db.execute("SELECT 1 FROM store.blob WHERE sha256=?", (sha,)).fetchone())
        finally:
            a.close()

    def test_downloader_replaces_a_damaged_same_size_file(self) -> None:
        from dtf_backup.media import Downloader

        class Client:
            def download(self, url, dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"good-bytes")

                class R:
                    headers = {"content-type": "image/png"}
                    sha256 = hashlib.sha256(b"good-bytes").hexdigest()
                    size = 10
                return R()
        store = self.lib / "media"
        sha = hashlib.sha256(b"good-bytes").hexdigest()
        final = store / sha[:2] / f"{sha}.png"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"BAD!-bytes")
        res = Downloader(Client(), store)._fetch("00000000-0000-4000-8000-000000000001", "png", [])
        self.assertEqual(res["via"], "download")
        self.assertEqual(final.read_bytes(), b"good-bytes")

    # ------------------------------------------------------------------ databases
    def test_damaged_database_restored_from_its_copy(self) -> None:
        self.assertTrue(integrity.db_backup_path(self.arch()).exists())       # the first sync made the daily copy
        self.fake.posts[2]["title"] = "Пост номер 2 (после копии)"
        self.fake.posts[2]["dateModified"] += 100
        self.assertEqual(self.sync(), 0)                                     # an edit the copy doesn't have
        a = self.arch()
        a.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page = a.db.execute("PRAGMA page_size").fetchone()[0]
        root = a.db.execute("SELECT rootpage FROM sqlite_master WHERE name='slices'").fetchone()[0]
        a.close()
        p = self.root / ".state" / "state.sqlite"
        b = bytearray(p.read_bytes())
        off = (root - 1) * page
        b[off:off + 12] = b"Z" * 12                                     # a b-tree page header garbled
        p.write_bytes(bytes(b))
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(list((self.root / ".state" / "quarantine").rglob("state.sqlite"))), 1)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM posts")[0]["n"], 8)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM my_comments")[0]["n"], 38)
        self.assertEqual(len(self.q("SELECT * FROM history WHERE kind='post' AND item_id=2 AND event='edit'")), 1)
        self.assertTrue(any(f["kind"] == "database" for f in self.log()))

    def test_backup_once_a_day(self) -> None:
        a = self.arch()
        try:
            first = a.get_meta(integrity.META_DB_BACKUP)
            self.assertFalse(integrity.backup_dbs(a))                        # taken today already
            self.assertTrue(integrity.backup_dbs(a, force=True))
            self.assertGreaterEqual(a.get_meta(integrity.META_DB_BACKUP), first)
            self.assertTrue(integrity.store_backup_path(self.lib).exists())
        finally:
            a.close()

    # ------------------------------------------------------------------ helpers
    def drop_copies(self, rel: str, view: bool = True) -> None:
        """No mirror (and no pages) to restore `rel` from."""
        (self.root / ".state" / "backup" / "raw" / rel).unlink(missing_ok=True)
        if view:
            (self.root / ".state" / "view.sqlite").unlink(missing_ok=True)


def read_json_gz_lines(p: Path) -> list[dict]:
    import json
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


class ReadRawTest(unittest.TestCase):
    def test_damage_is_not_absence(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json.gz"
            self.assertEqual(integrity.read_raw(p, {}), {})
            with self.assertRaises(FileNotFoundError):
                integrity.read_raw(p)
            p.write_bytes(gzip.compress(b'{"a": 1}')[:-6])
            with self.assertRaises(integrity.Damaged):
                integrity.read_raw(p, {})
            p.write_bytes(b"\x00\x01")
            with self.assertRaises(integrity.Damaged):
                integrity.read_raw(p, {})

    def test_record_damaged_once(self) -> None:
        from dtf_backup.history import record_damaged
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, kind TEXT, item_id INTEGER, entry_id INTEGER, "
                   "at INTEGER, event TEXT, state TEXT, version_date INTEGER, sig TEXT, body BLOB)")
        self.assertTrue(record_damaged(db, "post", 1, "abc", 5, "replaced"))
        self.assertFalse(record_damaged(db, "post", 1, "abc", 5, "lost"))
        self.assertTrue(record_damaged(db, "post", 1, None, 5, "lost"))
        self.assertEqual(content_sig("comment", {"text": "a", "media": []}),
                         content_sig("comment", copy.deepcopy({"text": "a", "media": [], "_site": {"x": 1}})))


if __name__ == "__main__":
    unittest.main()
