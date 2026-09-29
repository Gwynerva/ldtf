"""Archives of posts only (setting "scope"): nothing of comments is fetched, switching an archive to posts only drops
every comment it has (rows, raw files, their media) without leaving it half switched, and the pages have no comment
sections then."""

import gzip
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup.render import render  # noqa: E402
from dtf_backup.scope import comment_footprint, describe, drop_comments, has_comment_data, pending_drop  # noqa: E402
from dtf_backup.settings import save_settings  # noqa: E402
from dtf_backup.state import META_LAST_SYNC, Archive, gc_media, open_store  # noqa: E402
from dtf_backup.sync import Syncer  # noqa: E402
from dtf_backup.viewdb import view_meta, open_view  # noqa: E402
from dtf_backup.web.jobs import JobManager, plan_stages  # noqa: E402
from test_app import OWN_POST, UID, NoRedirect, make_archive  # noqa: E402

COMMENT_FILE = b"\x89PNG comment picture" + b"c" * 3000


def add_comment_data(arch: Archive) -> None:
    """What a synced archive of posts and comments has beside the raw files make_archive writes: rows, cursors,
    media references of comments (one file only comments use), a stopped-by-guard and a last-sync record."""
    db = arch.db
    db.execute("INSERT INTO posts(id, date, comments_count, tree_status, tree_count, tree_fetched_at) "
               "VALUES (?, 1790000000, 2, 'ok', 2, 1790000100)", (OWN_POST,))
    for cid, state in ((12, None), (22, "removed"), (24, None)):
        db.execute("INSERT INTO my_comments(id, entry_id, date, raw, site_state) VALUES (?, 1, 1790000000, ?, ?)",
                   (cid, b"x" * 100, state))
    db.execute("INSERT INTO slices(start, end, done) VALUES (1, 2, 1)")
    db.execute("INSERT INTO threads(entry_id, status) VALUES (6000001, 'ok')")
    sha = hashlib.sha256(COMMENT_FILE).hexdigest()
    f = arch.library / "media" / sha[:2] / f"{sha}.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(COMMENT_FILE)
    db.execute("INSERT INTO store.blob VALUES (?,?,?,?,?,?)", (sha, len(COMMENT_FILE), "png", "image/png", None,
                                                               f"{sha[:2]}/{sha}.png"))
    arch.queue_media([("comment-pic", None, "png")], "mc:24")
    arch.queue_media([("comment-pic", None, "png")], "tc:23")
    db.execute("UPDATE store.media_ref SET status='done', sha256=? WHERE key='comment-pic'", (sha,))
    # the post's picture is also in a comment under it: it stays (a post needs it)
    arch.queue_media([("11111111-2222-5333-8444-555555555555", None, "jpg")], "pc:11")
    arch.queue_media([("avatar-guest", None, "jpg")], "avatar:42")
    arch.queue_media([("avatar-own", None, "jpg")], f"avatar:{UID}")
    arch.queue_media([("reaction-1", None, "png")], "reaction:1")
    arch.set_meta("comments_backfill_done", True)
    arch.set_meta("raw_scanned_mtime", 123.0)
    arch.set_meta(META_LAST_SYNC, {"finished": 1790000200, "stats": {
        "comments": {"total": 3}, "posts": {"listed": 1},
        "site": {"posts_removed": 1, "comments_removed": 2, "context": 5},
        "site_items": [{"kind": "post", "id": OWN_POST}, {"kind": "comment", "id": 22}]}})
    arch.commit()


def owners(arch: Archive) -> set:
    return {r[0] for r in arch.db.execute("SELECT owner FROM main.media_use")}


class DropCommentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lib = Path(self.tmp.name) / "archive"
        self.arch = make_archive(self.lib)
        add_comment_data(self.arch)

    def tearDown(self) -> None:
        self.arch.close()
        self.tmp.cleanup()

    def test_footprint_counts_only_what_goes(self) -> None:
        self.assertTrue(has_comment_data(self.arch))
        fp = comment_footprint(self.arch)
        self.assertEqual((fp["comments"], fp["lost"], fp["trees"], fp["threads"], fp["files"]), (3, 1, 1, 1, 1))
        self.assertGreaterEqual(fp["bytes"], len(COMMENT_FILE) + 300)   # the file, the rows and the raw files
        self.assertEqual(describe(fp), "3 комментария пользователя, комментарии под 1 постом, 1 ветка обсуждений, "
                                       "1 файл, нужных только им")
        self.assertEqual(describe(dict(fp, comments=0, threads=0, files=0)), "комментарии под 1 постом")
        self.assertFalse(pending_drop(self.arch))                        # the archive keeps comments
        save_settings(self.arch.settings_path, {"scope": "posts"})
        self.assertTrue(pending_drop(self.arch))

    def test_drop_everything_of_comments(self) -> None:
        a = self.arch
        n = drop_comments(a)
        self.assertEqual((n["comments"], n["trees"], n["threads"]), (3, 1, 1))
        for t in ("my_comments", "slices", "threads"):
            self.assertEqual(a.db.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0], 0, t)
        row = a.db.execute("SELECT tree_status, tree_count, tree_fetched_at, comments_count FROM posts").fetchone()
        self.assertEqual(tuple(row), (None, None, None, 2))   # DTF's counter stays
        for d in ("post-trees", "threads", "my-comments"):
            self.assertFalse((a.raw / d).exists(), d)
        self.assertEqual(list(a.raw.glob(".trash-*")), [])
        self.assertIsNone(a.get_meta("comments_backfill_done"))
        self.assertIsNone(a.get_meta("raw_scanned_mtime"))
        st = a.get_meta(META_LAST_SYNC)["stats"]
        self.assertNotIn("comments", st)
        self.assertEqual(st["site"], {"posts_removed": 1})
        self.assertEqual(st["site_items"], [{"kind": "post", "id": OWN_POST}])
        self.assertEqual(owners(a), {f"post:{OWN_POST}", f"avatar:{UID}", "reaction:1"})
        self.assertFalse(has_comment_data(a))
        self.assertEqual(drop_comments(a)["comments"], 0)   # again: nothing to do, nothing breaks
        # the shared store frees the file only comments used; the post's picture stays
        a.close()
        self.assertEqual(gc_media(self.lib)["files"], 1)
        store = open_store(self.lib)
        self.assertEqual(store.execute("SELECT COUNT(*) FROM store.blob").fetchone()[0], 1)
        store.close()

    def test_build_of_posts_only_ignores_leftovers(self) -> None:
        """A late write of a stopped sync (or a drop cut short) must not bring comments back into the pages."""
        save_settings(self.arch.settings_path, {"scope": "posts"})
        self.arch.close()
        render(self.arch)
        db = open_view(self.arch)
        m = view_meta(db)
        self.assertEqual((m["counts"]["my_comments"], m["counts"]["post_comments"]), (0, 0))
        self.assertFalse(m["comments"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)
        db.close()
        self.assertFalse((self.arch.root / "data" / "comments.jsonl").exists())
        self.assertIn("хранит только посты", (self.arch.root / "README.md").read_text(encoding="utf-8"))

    def test_sync_drops_what_is_left_first(self) -> None:
        save_settings(self.arch.settings_path, {"scope": "posts"})
        s = Syncer.from_settings(self.arch, None, self.arch.settings(), only=["posts"])
        s.stage_profile = lambda: None               # offline: only the start of a sync is under test
        s.stage_posts = lambda pool: None
        self.assertEqual(s.run(), 0)
        self.assertIsNotNone(s.dropped)
        self.assertFalse(has_comment_data(self.arch))


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def timeline_page(self, uid, cursor):  # noqa: ANN001
        self.calls.append("timeline")
        return [{"id": 1, "date": 100, "dateModified": 100, "counters": {"comments": 5}}], None

    def content(self, pid):  # noqa: ANN001
        self.calls.append("content")
        return {"id": pid, "date": 100, "dateModified": 100, "title": "Пост", "blocks": []}

    def post_comments(self, pid):  # noqa: ANN001
        self.calls.append("comments")
        return []


class PostsStageTest(unittest.TestCase):
    def test_no_comment_trees_for_posts_only(self) -> None:
        for scope, trees in (("posts", 0), ("all", 1)):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                arch = Archive(Path(d) / "x", Path(d))
                arch.set_meta("user_id", 1)
                s = Syncer(arch, None, scope=scope)
                s.api = FakeApi()
                with ThreadPoolExecutor(2) as pool:
                    s.stage_posts(pool)
                self.assertEqual(s.api.calls.count("comments"), trees, scope)
                self.assertEqual(s.api.calls.count("content"), 1)
                self.assertEqual(arch.raw_post_tree(1).exists(), bool(trees))
                arch.close()


class JobsTest(unittest.TestCase):
    def wait(self, job, timeout: float = 60) -> None:
        end = time.monotonic() + timeout
        while job.state in ("queued", "running") and time.monotonic() < end:
            time.sleep(0.05)

    def test_stages_follow_the_settings(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d)
            a = make_archive(lib)
            a.close()
            self.assertEqual(plan_stages(a, "sync"), ["profile", "posts", "comments", "threads", "media", "build"])
            save_settings(a.settings_path, {"scope": "posts", "media": "off"})
            self.assertEqual(plan_stages(a, "sync"), ["purge", "profile", "posts", "build"])   # comments still there
            self.assertEqual(plan_stages(a, "purge"), ["purge", "build"])
            self.assertEqual(plan_stages(a, "render"), ["build"])

    def test_purge_job(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lib = Path(d) / "archive"
            a = make_archive(lib)
            add_comment_data(a)
            a.close()
            jm = JobManager(lib)
            try:
                j = jm.submit("tester", "purge", reason="manual")   # scope is still "all": nothing to drop
                self.wait(j)
                self.assertEqual((j.state, [s["status"] for s in j.snapshot()["stages"]]), ("done", ["skipped", "skipped"]))
                self.assertTrue(has_comment_data(a))
                save_settings(a.settings_path, {"scope": "posts"})
                j = jm.submit("tester", "purge", reason="manual")
                self.assertFalse(j.snapshot()["stoppable"])
                self.wait(j)
                self.assertEqual(j.state, "done", j.snapshot())
                self.assertEqual([s["key"] for s in j.snapshot()["stages"]], ["purge", "build"])
                self.assertFalse(has_comment_data(a))
                self.assertFalse(any((lib / "media").rglob("*.png")))   # the file only comments used is freed
                db = open_view(a)
                self.assertEqual(view_meta(db)["counts"]["my_comments"], 0)
                db.close()
            finally:
                jm.stop(5)
                a.close()

    def test_one_job_per_archive_and_gc_holds_the_start(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            live: dict[str, int] = {}
            peak: dict[str, int] = {}
            gate = threading.Lock()

            class Jobs(JobManager):
                def _run(self, job) -> None:  # noqa: ANN001
                    job.state = "running"
                    with gate:
                        live[job.nick] = live.get(job.nick, 0) + 1
                        peak[job.nick] = max(peak.get(job.nick, 0), live[job.nick])
                    time.sleep(0.2)
                    with gate:
                        live[job.nick] -= 1
                    job.state = "done"

            jm = Jobs(Path(d), max_parallel=2)
            try:
                a = jm.submit("a", "sync", reason="schedule")
                b = jm.submit("a", "purge", reason="manual")   # not merged with the sync, not run beside it
                self.assertIsNot(a, b)
                self.wait(a)
                self.wait(b)
                self.assertEqual(peak["a"], 1)
                with jm.cond:
                    jm.gc_running = True                    # a cleanup of the shared store is running
                c = jm.submit("c", "sync", reason="manual")
                time.sleep(0.3)
                self.assertEqual(c.state, "queued")          # nothing starts meanwhile
                with jm.cond:
                    jm.gc_running = False
                    jm.cond.notify_all()
                self.wait(c)
                self.assertEqual(c.state, "done")
            finally:
                jm.stop(5)


class NoticeTest(unittest.TestCase):
    def test_only_what_matters(self) -> None:
        from dtf_backup.desktop import notice

        class J:
            def __init__(self, state: str, reason: str, kind: str = "sync") -> None:
                self.kind, self.state, self.nick, self.params = kind, state, "petra", {"reason": reason}
                self.started, self.finished, self.error = 0.0, 120.0, "сеть недоступна"

        self.assertIsNone(notice(J("done", "schedule"), None, {"context": 12}))      # routine: quiet
        self.assertIsNotNone(notice(J("done", "manual"), None, None))               # the user is waiting for it
        self.assertIn("1 пост", notice(J("done", "schedule"), None, {"posts_removed": 1})[1])
        self.assertIsNotNone(notice(J("blocked", "schedule"), None, None))
        self.assertIsNotNone(notice(J("error", "schedule"), {"fails": 1}, None))    # the first failure
        self.assertIsNone(notice(J("error", "schedule"), {"fails": 3}, None))       # not again on every retry
        self.assertIsNone(notice(J("done", "manual", kind="render"), None, None))


class PostsOnlyAppTest(unittest.TestCase):
    """The pages of an archive of posts only, and switching an archive with comments to posts only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.library = Path(cls.tmp.name) / "archive"
        posts = make_archive(cls.library, "posts")
        posts.close()
        save_settings(posts.settings_path, {"scope": "posts"})
        render(posts)
        full = make_archive(cls.library, "full")
        add_comment_data(full)
        full.close()
        render(full)
        from dtf_backup.web.server import App, Handler
        cls.app = App(cls.library, 0)
        Handler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.jobs.stop(5)
        cls.app.invalidate()
        cls.tmp.cleanup()

    def get(self, path: str) -> tuple[int, str, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{urllib.parse.quote(path, safe='/?=&')}")
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def post(self, path: str, data: dict) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=urllib.parse.urlencode(data).encode())
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def test_pages_without_comments(self) -> None:
        code, body, _ = self.get("/u/posts/")
        self.assertEqual(code, 200)
        nav = re.search(r'<nav class="dest".*?</nav>', body, re.S).group(0)
        self.assertNotIn("/u/posts/comments", nav)
        self.assertEqual(len(re.findall(r'class="dest-i[ "]', nav)), 3)
        self.assertNotIn("Комментарии по годам", body)
        for path in ("/u/posts/comments", "/u/posts/c/2026-09", "/u/posts/go/c/12"):
            code, _, h = self.get(path)
            self.assertEqual((code, h.get("Location")), (302, "/u/posts/"), path)
        _, body, _ = self.get(f"/u/posts/p/{OWN_POST}")
        self.assertIn("Комментарии в этом архиве не сохраняются", body)
        self.assertIn("2 комментария на DTF", body)
        _, body, _ = self.get("/u/posts/search?q=катана")
        self.assertNotIn('name="t"', body)
        self.assertIn("Поиск по постам", body)
        _, body, _ = self.get("/archives")
        self.assertIn("только посты", body)
        _, body, _ = self.get("/u/posts/sync")
        self.assertNotIn("<span>комментари", body)

    def test_switch_asks_first_then_drops(self) -> None:
        fields = {"_csrf": self.app.csrf, "scope": "posts", "media": "all", "schedule": "off", "schedule_hours": "5"}
        code, _, _ = self.get("/u/full/settings")
        self.assertEqual(code, 200)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/u/full/settings",
                                     data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8")
        self.assertIn("Перейти на «Только посты»?", body)
        self.assertIn("уже удалены на DTF", body)             # one of them exists only here
        self.assertIn('name="schedule_hours" value="5"', body)  # the other edits come along
        arch = self.app.archive("full")
        self.assertEqual(arch.settings()["scope"], "all")       # nothing changed yet
        code, h = self.post("/u/full/settings", {**fields, "confirm_drop": "1"})
        self.assertEqual((code, h.get("Location")), (303, "/u/full/sync"))
        self.assertEqual((arch.settings()["scope"], arch.settings()["schedule_hours"]), ("posts", 5))
        job = self.app.jobs.for_nick("full")
        self.assertEqual(job.kind, "purge")
        for _ in range(400):
            if job.state not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job.state, "done", job.snapshot())
        self.assertFalse(has_comment_data(arch))
        _, body, _ = self.get("/u/full/")
        self.assertNotIn("/u/full/comments", re.search(r'<nav class="dest".*?</nav>', body, re.S).group(0))
        # and back: comments come with the next sync
        code, h = self.post("/u/full/settings", {**fields, "scope": "all"})
        self.assertEqual(code, 303)
        _, body, _ = self.get("/u/full/settings")
        self.assertIn("Комментарии загрузятся при следующей синхронизации", body)


if __name__ == "__main__":
    unittest.main()
