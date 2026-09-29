"""Archive state without surprises: the shared media cleanup never deletes what it can't prove unused, meta is read
without opening archives for writing, settings of older versions keep their meaning, and the media mode
"posts" downloads exactly the post side of an archive."""

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup import sync as sync_mod  # noqa: E402
from dtf_backup.media import COMMENT_OWNERS, POSTS_ONLY_OWNERS, lookup, owner_kind  # noqa: E402
from dtf_backup.scheduler import next_sync  # noqa: E402
from dtf_backup.settings import clean_settings, load_settings, save_settings  # noqa: E402
from dtf_backup.state import (Archive, connect_ro, gc_media, gc_pending, open_store, read_meta,  # noqa: E402
                              state_db, store_path)
from dtf_backup.sync import Syncer, acquire_lock, release_lock  # noqa: E402
from test_app import make_archive  # noqa: E402


def blobs(lib: Path) -> int:
    return sum(1 for p in (lib / "media").rglob("*") if p.is_file())


class GcSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lib = Path(self.tmp.name) / "archive"
        make_archive(self.lib, "one").close()
        make_archive(self.lib, "two").close()
        import shutil
        shutil.rmtree(self.lib / "one")   # "two" still needs the shared file

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_running_sync_postpones(self) -> None:
        r = gc_media(self.lib, busy=True)
        self.assertEqual(r["status"], "postponed")
        self.assertTrue(gc_pending(self.lib).exists())
        lock = Archive(self.lib / "two", self.lib).lock_path
        self.assertEqual(acquire_lock(lock), 0)   # a sync of "two" (here: this process) holds the lock
        try:
            self.assertEqual(gc_media(self.lib)["status"], "postponed")
        finally:
            release_lock(lock)
        self.assertEqual(gc_media(self.lib)["status"], "done")
        self.assertFalse(gc_pending(self.lib).exists())
        self.assertEqual(blobs(self.lib), 1)

    def test_unreadable_archive_keeps_everything(self) -> None:
        make_archive(self.lib, "three").close()
        state_db(self.lib / "three").write_bytes(b"this is not a database" * 100)   # damaged: its needs are unknown
        import shutil
        shutil.rmtree(self.lib / "two")   # nothing readable uses the file any more
        r = gc_media(self.lib)
        self.assertEqual(r["status"], "postponed")
        self.assertIn("three", r["reason"])
        self.assertEqual(blobs(self.lib), 1)
        self.assertFalse((self.lib / "three" / ".state" / "sync.lock").exists())   # locks released

    def test_missing_table_keeps_everything(self) -> None:
        make_archive(self.lib, "three").close()
        con = sqlite3.connect(state_db(self.lib / "three"))
        con.execute("DROP TABLE media_use")
        con.execute("CREATE VIEW media_use AS SELECT 1 AS nope")   # a table the query can't read
        con.commit()
        con.close()
        import shutil
        shutil.rmtree(self.lib / "two")
        self.assertEqual(gc_media(self.lib)["status"], "postponed")
        self.assertEqual(blobs(self.lib), 1)


class MetaTest(unittest.TestCase):
    def test_read_meta_and_odd_paths(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d) / "архив #1 100% x"   # '#', '%', space and Cyrillic in the path
            a = make_archive(lib, "tester")
            a.set_meta("guard", {"kind": "posts-mass"})
            a.commit()
            a.close()
            self.assertEqual(read_meta(lib / "tester", ("user_id", "guard", "nope")),
                             {"user_id": 777, "guard": {"kind": "posts-mass"}})
            con = connect_ro(state_db(lib / "tester"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM meta").fetchone()[0] > 0, True)
            con.close()
            self.assertEqual(read_meta(lib / "nobody", ("guard",)), {})
            self.assertIsNone(next_sync(lib / "tester"))   # stopped by the guard: not scheduled

    def test_unreadable_meta_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d)
            make_archive(lib, "tester").close()
            self.assertIsNotNone(next_sync(lib / "tester"))   # never synced: due now
            state_db(lib / "tester").write_bytes(b"garbage" * 200)
            self.assertIsNone(read_meta(lib / "tester", ("guard",)))
            self.assertIsNone(next_sync(lib / "tester"))       # can't read: never guess "now"

    def test_lookup_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d)
            self.assertEqual(lookup(lib, ["x", None]), {})
            self.assertFalse(store_path(lib).exists())
            a = make_archive(lib, "tester")
            a.close()
            got = lookup(lib, ["11111111-2222-5333-8444-555555555555", "other"])
            self.assertEqual(list(got), ["11111111-2222-5333-8444-555555555555"])
            self.assertTrue(got["11111111-2222-5333-8444-555555555555"]["path"].startswith("media/"))


class SettingsCompatTest(unittest.TestCase):
    def test_media_switch_of_older_versions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            for old, new in ((True, "all"), (False, "off"), ("posts", "posts"), ("bogus", "all")):
                p.write_text(json.dumps({"media": old}), encoding="utf-8")
                self.assertEqual(load_settings(p)["media"], new, old)
            for form, new in (("1", "all"), ("on", "all"), ("0", "off"), ("", "off"), ("posts", "posts")):
                self.assertEqual(clean_settings({"media": form}, {"media": "all"})["media"], new, form)
            p.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_settings(p)["media"], "all")   # damaged file: defaults (and a log line)

    def test_syncer_modes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            p = Path(d) / "settings.json"
            arch = Archive(Path(d) / "x", Path(d))
            for mode, stage in (("all", True), ("posts", True), ("off", False)):
                s = save_settings(p, {"media": mode})
                sy = Syncer.from_settings(arch, None, s, workers=None)
                self.assertEqual((sy.media, "media" in sy.only), (mode, stage))
                self.assertEqual(sy.only & {"comments", "threads"}, {"comments", "threads"})
            self.assertEqual(Syncer(arch, None, no_media=True).media, "off")
            # the CLI's network flags keep their bounds (a 0 would break the thread pool)
            sy = Syncer(arch, None, workers=0, media_workers=99, rate=500)
            self.assertEqual((sy.workers, sy.media_workers), (1, 16))

    def test_posts_only_scope(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            arch = Archive(Path(d) / "x", Path(d))
            s = save_settings(Path(d) / "settings.json", {"scope": "posts", "media": "all"})
            sy = Syncer.from_settings(arch, None, s)
            self.assertEqual(sy.only, {"posts", "media"})    # no comment stages at all
            self.assertEqual(sy.media, "posts")              # no comment media, even leftovers in the queue
            sy = Syncer.from_settings(arch, None, s, media="off")
            self.assertEqual(sy.only, {"posts"})
            with self.assertRaises(SystemExit):              # asked explicitly: say why it can't
                Syncer.from_settings(arch, None, s, only=["comments"])


class FakeDownloader:
    fetched: list[str] = []

    def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
        pass

    def fetch(self, key: str, kind, candidates) -> dict:  # noqa: ANN001
        FakeDownloader.fetched.append(key)
        return {"status": "missing", "error": "HTTP 404"}


class PostsOnlyModeTest(unittest.TestCase):
    OWNERS = {"k-profile": "profile", "k-avatar": "avatar:1", "k-reaction": "reaction:5", "k-badge": "badge:2",
              "k-post": "post:10", "k-pc": "pc:100", "k-mc": "mc:200", "k-tc": "tc:300"}

    def test_downloads_only_the_post_side(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d)
            arch = Archive(lib / "tester", lib)
            for key, owner in self.OWNERS.items():
                arch.queue_media([(key, None, "jpg")], owner)
            arch.queue_media([("k-shared", None, "jpg")], "mc:201")    # the same file in a comment...
            arch.queue_media([("k-shared", None, "jpg")], "post:11")   # ...and in a post: downloaded
            arch.commit()
            orig = sync_mod.Downloader
            sync_mod.Downloader = FakeDownloader
            try:
                FakeDownloader.fetched = []
                s = Syncer(arch, None, media="posts")
                with ThreadPoolExecutor(2) as pool:
                    s.stage_media(pool)
            finally:
                sync_mod.Downloader = orig
            self.assertEqual(sorted(FakeDownloader.fetched),
                             ["k-avatar", "k-badge", "k-post", "k-profile", "k-reaction", "k-shared"])
            self.assertEqual(s.stats["media"]["skipped_by_mode"], 3)
            arch.close()

    def test_every_owner_kind_is_classified(self) -> None:
        src = (Path(__file__).resolve().parent.parent / "dtf_backup" / "sync.py").read_text(encoding="utf-8")
        owners = re.findall(r'queue_media\(.*,\s*f?"([^"]*)"\)', src)   # the last argument of each call
        self.assertGreaterEqual(len(owners), 7)
        kinds = {owner_kind(o) for o in owners if not o.startswith("{")}
        kinds |= {"reaction", "badge"}   # f"{kind[:-1]}:..." over ("reactions", "badges")
        self.assertTrue(kinds <= set(POSTS_ONLY_OWNERS) | set(COMMENT_OWNERS), kinds)
        self.assertEqual(kinds, set(POSTS_ONLY_OWNERS) | set(COMMENT_OWNERS))


if __name__ == "__main__":
    unittest.main()
