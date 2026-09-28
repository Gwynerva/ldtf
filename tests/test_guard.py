"""Archive guard: DTF placeholders never overwrite the archive; account problems and mass losses stop the sync (offline)."""

import copy
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.guard import (GuardTrip, account_problem, comment_stub, degraded, merge_items, post_loss,  # noqa: E402
                              post_stub, post_wiped, posts_limit, site_summary)
from dtf_backup.http import HttpError  # noqa: E402
from dtf_backup.state import Archive, unpack  # noqa: E402
from dtf_backup.sync import Syncer  # noqa: E402
from dtf_backup.util import read_json_gz  # noqa: E402

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "guard" / "api_samples.json").read_text(encoding="utf-8"))
LONG = ("Длинный содержательный текст поста про игры, мечи и косплей, который точно стоит сохранить в архиве. " * 6).strip()


class DetectTest(unittest.TestCase):
    def test_accounts(self) -> None:
        self.assertEqual(account_problem(SAMPLES["profile_deleted"]), "deleted")
        self.assertEqual(account_problem(SAMPLES["profile_frozen"]), "frozen")
        self.assertIsNone(account_problem({"id": 1, "name": "Петр", "nickname": "petra", "uri": "/petra"}))
        # the placeholder name is only trusted without a nickname (a live joke account keeps its nick)
        self.assertIsNone(account_problem({"id": 2, "name": "Аккаунт удален", "nickname": "joker", "uri": "/joker"}))
        self.assertEqual(account_problem({"id": 3, "name": "Аккаунт удалён", "nickname": None, "uri": ""}), "deleted")

    def test_posts(self) -> None:
        good = {"id": 1, "title": "Прошел игру", "blocks": [{"type": "text", "data": {"text": f"<p>{LONG}</p>"}}] * 3}
        self.assertEqual(post_stub(SAMPLES["post_removed_by_request"]), "removed")
        self.assertEqual(post_stub(SAMPLES["timeline_item_removed"]), "removed")
        self.assertEqual(post_stub(SAMPLES["post_wiped_by_edit"]), "removed")   # title "Статья удалена"
        self.assertIsNone(post_stub(good))
        # a live author edits the text away under a normal title
        wiped = {"id": 1, "title": "Прошел игру", "blocks": [{"type": "text", "data": {"text": "<p>.</p>"}}]}
        self.assertTrue(post_wiped(good, wiped))
        self.assertEqual(post_loss(good, wiped), "wiped")
        self.assertEqual(post_loss(good, SAMPLES["post_removed_by_request"]), "removed")
        # an ordinary edit, a short post and a first download are not losses
        edited = copy.deepcopy(good)
        edited["blocks"] = edited["blocks"][:2]
        self.assertIsNone(post_loss(good, edited))
        self.assertIsNone(post_loss(None, SAMPLES["post_removed_by_request"]))
        short = {"id": 2, "title": "Мем", "blocks": [{"type": "text", "data": {"text": "лол"}}]}
        self.assertIsNone(post_loss(short, {"id": 2, "title": "Мем", "blocks": []}))

    def test_comments(self) -> None:
        cs = SAMPLES["comments"]
        self.assertEqual(comment_stub(cs["moderator"]), "moderator")
        self.assertEqual(comment_stub(cs["post_author"]), "removed")
        self.assertEqual(comment_stub(cs["author_deleted"]), "author-deleted")
        self.assertEqual(comment_stub(cs["author_frozen"]), "author-frozen")
        self.assertEqual(comment_stub(cs["unavailable_no_flags"]), "unavailable")
        real = {"id": 1, "text": "Нормальный комментарий по делу, достаточно длинный для проверки", "author": {"id": 5}}
        self.assertIsNone(comment_stub(real))
        self.assertIsNone(comment_stub({"id": 2, "text": "", "media": [{"type": "image"}]}))   # picture-only comment
        self.assertEqual(degraded(real, {**real, "text": "Комментарий недоступен"}), "unavailable")
        self.assertEqual(degraded(real, {**real, "text": "."}), "wiped")
        self.assertIsNone(degraded(real, {**real, "text": real["text"] + " (дополнено)"}))
        self.assertIsNone(degraded({"id": 3, "text": "ок"}, {"id": 3, "text": "."}))   # nothing worth keeping
        self.assertIsNone(degraded(cs["moderator"], cs["moderator"]))

    def test_merge(self) -> None:
        old = [{"id": 1, "date": 10, "text": "Корень ветки, очень интересное мнение", "author": {"id": 7}},
               {"id": 2, "date": 20, "text": "Мой ответ на корень ветки, тоже важный", "author": {"id": 5}, "replyTo": 1},
               {"id": 3, "date": 30, "text": "Ещё один ответ, который потом пропадёт", "author": {"id": 8}, "replyTo": 1}]
        new = [{**old[0], "text": "Комментарий недоступен", "author": {"id": 7, "isRemovedByUserRequest": True}},
               old[1], {"id": 4, "date": 40, "text": "Новый ответ", "author": {"id": 9}, "replyTo": 1}]
        merged, st = merge_items(old, new, 100, uid=5)
        by = {c["id"]: c for c in merged}
        self.assertEqual(by[1]["text"], old[0]["text"])
        self.assertEqual(by[1]["_site"], {"state": "author-deleted", "at": 100})
        self.assertEqual(by[3]["_site"]["state"], "gone")
        self.assertNotIn("_site", by[4])
        self.assertEqual([c["id"] for c in merged], [1, 2, 3, 4])
        self.assertEqual((st["kept"], st["gone"], st["mine"]), (1, 1, 0))
        # the next sync sees the same placeholders: nothing new is counted, the archived text stays
        again, st2 = merge_items(merged, new, 200, uid=5)
        self.assertEqual({c["id"]: c.get("text") for c in again}[1], old[0]["text"])
        self.assertEqual(sum(st2.values()), 0)

    def test_limits_and_summary(self) -> None:
        self.assertEqual(posts_limit(8), 3)
        self.assertEqual(posts_limit(112), 10)
        self.assertEqual(site_summary({"posts_removed": 1, "comments_gone": 2, "comments_wiped": 1, "context": 5}),
                         "1 пост, 3 комментария, 5 чужих комментариев в ветках")
        self.assertEqual(site_summary({}), "")


# ---------------------------------------------------------------------- fake DTF
UID = 500
T0 = int(time.time()) - 5 * 86400


class FakeDtf:
    """In-memory DTF with the shapes of the real API. Tests mutate `posts`, `trees`, `feed` and `branches`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.profile = {"id": UID, "name": "Тестовый Автор", "nickname": "tester", "uri": "/tester", "created": T0 - 86400,
                        "isFrozen": False, "isRemovedByUserRequest": False, "avatar": None, "counters": {}}
        self.renamed_to: str | None = None
        self.posts: dict[int, dict] = {}
        self.hidden: set[int] = set()          # not in the timeline
        self.content_404: set[int] = set()
        self.trees: dict[int, list[dict]] = {}
        self.feed: list[dict] = []             # the user's comments, newest first
        self.branches: dict[int, list[dict]] = {}   # foreign post id -> its thread
        cid = 1000
        for i in range(1, 9):
            pid = i
            self.posts[pid] = {"id": pid, "title": f"Пост номер {i}", "date": T0 + i * 3600, "dateModified": T0 + i * 3600,
                               "url": f"https://dtf.ru/tester/{pid}", "counters": {"comments": 2}, "repostId": None,
                               "blocks": [{"type": "text", "data": {"text": f"<p>{LONG} ({i})</p>"}}],
                               "author": {"id": UID}, "subsite": {"id": UID}}
            cid += 1
            other = {"id": cid, "date": T0 + i * 3600 + 60, "text": f"Чужой комментарий к посту {i}, с мнением и доводами",
                     "author": {"id": 900 + i, "name": f"Гость {i}"}, "replyTo": 0, "level": 0, "entry": {"id": pid}}
            cid += 1
            mine = {"id": cid, "date": T0 + i * 3600 + 120, "text": f"Мой ответ гостю под своим постом {i}, развёрнутый",
                    "author": {"id": UID}, "replyTo": cid - 1, "level": 1, "replyCount": 0, "threadId": f"t{cid - 1}",
                    "entry": {"id": pid, "title": f"Пост номер {i}"}}
            self.trees[pid] = [other, mine]
            self.feed.append(mine)
        for j in range(30):   # replies in other people's posts: need context threads
            eid = 100 + j
            cid += 1
            root = {"id": cid, "date": T0 + 86400 + j * 600, "text": f"Корень чужой ветки {j}, о чём-то спорном",
                    "author": {"id": 700 + j, "name": f"Собеседник {j}"}, "replyTo": 0, "level": 0,
                    "threadId": f"r{cid}", "entry": {"id": eid},
                    "media": [{"type": "image", "data": {"uuid": f"00000000-0000-4000-8000-{j:012d}", "type": "jpg",
                                                         "width": 10, "height": 10, "size": 100}}]}
            cid += 1
            mine = {"id": cid, "date": T0 + 86400 + j * 600 + 60, "text": f"Мой подробный ответ в чужой ветке номер {j}, с аргументами",
                    "author": {"id": UID}, "replyTo": root["id"], "level": 1, "replyCount": 0, "threadId": f"r{root['id']}",
                    "entry": {"id": eid, "title": f"Чужой пост {j}"}}
            self.branches[eid] = [root, mine]
            self.feed.append(mine)
        self.feed.sort(key=lambda c: (c["date"], c["id"]), reverse=True)

    def _tick(self) -> None:
        with self.lock:
            self.calls += 1

    # --- the Dtf interface used by Syncer
    def subsite(self, ident: str) -> dict:
        self._tick()
        if self.renamed_to and ident in ("tester", "@tester"):
            raise HttpError(404, "subsite")
        if ident.isdigit() and int(ident) != UID:
            raise HttpError(404, "subsite")
        return copy.deepcopy(self.profile)

    def assets(self) -> dict:
        return {"reactions": [], "badges": []}

    def timeline_page(self, uid: int, cursor=None):
        self._tick()
        return [copy.deepcopy(p) for pid, p in sorted(self.posts.items(), reverse=True) if pid not in self.hidden], None

    def content(self, pid: int) -> dict:
        self._tick()
        if pid in self.content_404 or pid not in self.posts:
            raise HttpError(404, f"content {pid}")
        return copy.deepcopy(self.posts[pid])

    def post_comments(self, pid: int) -> list[dict]:
        self._tick()
        return copy.deepcopy(self.trees.get(pid, []))

    def comment_branch(self, cid: int) -> list[dict]:
        self._tick()
        for items in self.branches.values():
            if any(c["id"] == cid for c in items):
                return copy.deepcopy(items)
        raise HttpError(404, f"branch {cid}")

    def user_comments_page(self, uid: int, last_id=None, last_sv=None):
        self._tick()
        items = self.feed
        if last_sv is not None:
            items = [c for c in items if c["date"] < last_sv or (c["date"] == last_sv and c["id"] < (last_id or 0))]
        page = copy.deepcopy(items[:30])
        return page, (page[-1]["id"] if page else None), (page[-1]["date"] if page else None)

    # --- helpers for the tests
    def remove_account(self, frozen: bool = False) -> None:
        self.profile.update(name="Аккаунт заморожен" if frozen else "Аккаунт удален", nickname=None, uri="",
                            isFrozen=frozen, isRemovedByUserRequest=not frozen)
        stub = SAMPLES["timeline_item_removed"]
        for p in self.posts.values():
            p.update(title=stub["title"], blocks=copy.deepcopy(stub["blocks"]), isRemovedByUserRequest=True,
                     counters={"comments": 0})
        for c in self.feed + [c for t in self.trees.values() for c in t] + [c for b in self.branches.values() for c in b]:
            if (c.get("author") or {}).get("id") == UID:
                c["text"] = "Комментарий недоступен"
                c["author"] = {"id": UID, "name": self.profile["name"], "isRemovedByUserRequest": not frozen,
                               "isFrozen": frozen}
        self.trees = {pid: [] for pid in self.trees}

    def my_comment(self, i: int) -> dict:
        return self.feed[i]


def tree_hash(root: Path, materials_only: bool = False) -> dict[str, str]:
    """sha256 of every raw file; `materials_only` skips the profile and the reaction catalog (refreshed before the
    posts are compared)."""
    skip = {"assets.json.gz", "profile.json.gz"} if materials_only else set()
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.name not in skip}


class GuardSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lib = Path(self.tmp.name)
        self.fake = FakeDtf()
        self.assertEqual(self.sync(user="tester"), 0)
        self.arch = Archive(self.lib / "tester", self.lib)

    def tearDown(self) -> None:
        import gc
        self.arch.close()
        gc.collect()   # sqlite handles of rendered views
        self.tmp.cleanup()

    def sync(self, user=None, accept=False) -> int:
        arch = Archive(self.lib / "tester", self.lib)
        try:
            s = Syncer(arch, user, workers=3, no_media=True, refresh_days=30, accept=accept)
            s.api = self.fake
            self.last = s
            return s.run()
        finally:
            arch.close()

    def meta(self, key):
        v = self.arch.get_meta(key)
        self.arch.close()
        return v

    def raw_post(self, pid):
        return read_json_gz(self.arch.raw_post(pid))

    def my_raw(self, cid):
        row = self.arch.db.execute("SELECT raw, site_state FROM my_comments WHERE id=?", (cid,)).fetchone()
        self.arch.close()
        return unpack(row[0]), row[1]

    def test_baseline(self) -> None:
        db = self.arch.db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 8)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM my_comments").fetchone()[0], 38)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM threads WHERE status='ok'").fetchone()[0], 30)
        self.assertIsNone(self.arch.get_meta("guard"))
        self.arch.close()
        self.assertEqual(self.sync(), 0)   # a second sync with nothing changed changes nothing
        self.assertEqual(self.last.site, {})

    def test_deleted_account_stops_before_writing(self) -> None:
        before = tree_hash(self.lib / "tester" / "raw")
        state = self.arch.db.execute("SELECT * FROM posts ORDER BY id").fetchall()
        state = [tuple(r) for r in state]
        self.arch.close()
        self.fake.remove_account()
        self.assertEqual(self.sync(), 4)
        self.assertEqual(tree_hash(self.lib / "tester" / "raw"), before)          # not a byte changed
        self.assertEqual([tuple(r) for r in self.arch.db.execute("SELECT * FROM posts ORDER BY id")], state)
        g = self.meta("guard")
        self.assertEqual(g["kind"], "account-deleted")
        self.assertIn("Тестовый Автор", g["message"])
        self.assertEqual(self.meta("user_name"), "Тестовый Автор")
        # while stopped, syncs do not even ask DTF; accepting does not apply to account problems
        calls = self.fake.calls
        self.assertEqual(self.sync(), 4)
        self.assertEqual(self.sync(accept=True), 4)
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(tree_hash(self.lib / "tester" / "raw"), before)

    def test_frozen_account(self) -> None:
        self.fake.remove_account(frozen=True)
        self.assertEqual(self.sync(), 4)
        self.assertEqual(self.meta("guard")["kind"], "account-frozen")

    def test_missing_account_and_rename(self) -> None:
        self.fake.renamed_to = "renamed"        # the old nickname answers 404, the id still works
        self.fake.profile.update(nickname="renamed", uri="/renamed")
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.meta("user_ident"), str(UID))
        self.fake.profile["id"] = UID            # now the id answers 404 too
        self.fake.subsite = lambda ident: (_ for _ in ()).throw(HttpError(404, "subsite"))
        self.assertEqual(self.sync(), 4)
        self.assertEqual(self.meta("guard")["kind"], "account-missing")

    def test_single_losses_are_kept_and_marked(self) -> None:
        original = self.raw_post(3)
        self.fake.posts[3].update(title="Статья удалена", isRemovedByUserRequest=True,
                                  blocks=copy.deepcopy(SAMPLES["timeline_item_removed"]["blocks"]))
        c1, c2 = self.fake.my_comment(0), self.fake.my_comment(1)
        text1, text2 = c1["text"], c2["text"]
        c1["text"] = "Комментарий недоступен"
        c2["text"] = "."
        guest = self.fake.trees[5][0]
        guest_text = guest["text"]
        guest.update(text="Комментарий недоступен", author={"id": guest["author"]["id"], "isRemovedByUserRequest": True})
        self.fake.posts[5]["dateModified"] += 10   # makes the tree refresh
        self.assertEqual(self.sync(), 0)
        self.assertIsNone(self.meta("guard"))
        kept = self.raw_post(3)
        self.assertEqual(kept["title"], original["title"])
        self.assertEqual(kept["blocks"], original["blocks"])
        self.assertEqual(kept["_site"]["state"], "removed")
        self.assertEqual(self.arch.db.execute("SELECT site_state FROM posts WHERE id=3").fetchone()[0], "removed")
        self.arch.close()
        raw1, st1 = self.my_raw(c1["id"])
        raw2, st2 = self.my_raw(c2["id"])
        self.assertEqual((raw1["text"], st1), (text1, "unavailable"))
        self.assertEqual((raw2["text"], st2), (text2, "wiped"))
        tree = {c["id"]: c for c in read_json_gz(self.arch.raw_post_tree(5))["items"]}
        self.assertEqual(tree[guest["id"]]["text"], guest_text)
        self.assertEqual(tree[guest["id"]]["_site"]["state"], "author-deleted")
        self.assertEqual(self.last.site["posts_removed"], 1)
        self.assertEqual(self.last.site["comments_unavailable"] + self.last.site["comments_wiped"], 2)
        self.assertGreaterEqual(self.last.site["context"], 1)
        items = {(x["kind"], x["id"]): x for x in self.last.stats["site_items"]}
        self.assertEqual(items[("post", 3)]["state"], "removed")
        self.assertEqual(items[("comment", c2["id"])]["title"], text2)
        # the next sync sees the same placeholders and reports nothing new
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.last.site.get("posts_removed", 0) + self.last.site.get("comments_wiped", 0), 0)
        # the render shows the marks
        from dtf_backup.render import render
        from dtf_backup.viewdb import open_view
        a2 = Archive(self.lib / "tester", self.lib)
        render(a2)
        a2.close()
        v = open_view(a2)
        self.assertEqual(v.execute("SELECT site FROM posts WHERE id=3").fetchone()[0], "removed")
        v.close()
        rows = [json.loads(line) for line in (self.lib / "tester" / "data" / "comments.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["id"]: r["siteState"] for r in rows}[c2["id"]], "wiped")

    def test_mass_post_loss_needs_confirmation(self) -> None:
        before = tree_hash(self.lib / "tester" / "raw", materials_only=True)
        self.fake.hidden |= {1, 2, 3, 4}
        self.fake.content_404 |= {1, 2, 3}         # 4 is only hidden from the profile
        self.assertEqual(self.sync(), 4)
        g = self.meta("guard")
        self.assertEqual(g["kind"], "posts-mass")
        self.assertEqual(g["details"]["lost"], 4)
        self.assertEqual(len(g["details"]["samples"]), 4)
        self.assertEqual(tree_hash(self.lib / "tester" / "raw", materials_only=True), before)
        self.assertEqual(self.sync(), 4)            # waits for the user
        self.assertEqual(self.sync(accept=True), 0)
        self.assertIsNone(self.meta("guard"))
        states = dict(self.arch.db.execute("SELECT id, site_state FROM posts").fetchall())
        self.arch.close()
        self.assertEqual([states[i] for i in (1, 2, 3, 4, 5)], ["gone", "gone", "gone", None, None])
        self.assertEqual(self.raw_post(1)["title"], "Пост номер 1")
        self.assertEqual(self.sync(), 0)            # nothing new: no second stop

    def test_mass_comment_loss(self) -> None:
        for c in self.fake.feed[:25]:
            c["text"] = "."
        self.assertEqual(self.sync(), 4)
        g = self.meta("guard")
        self.assertEqual(g["kind"], "comments-mass")
        self.assertGreaterEqual(g["details"]["lost"], 20)
        for c in self.fake.feed[:25]:
            raw, _ = self.my_raw(c["id"])
            self.assertNotEqual(raw["text"], ".")
        self.assertEqual(self.sync(accept=True), 0)
        n = self.arch.db.execute("SELECT COUNT(*) FROM my_comments WHERE site_state='wiped'").fetchone()[0]
        self.arch.close()
        self.assertEqual(n, 25)

    def test_vanished_comments_are_marked_gone(self) -> None:
        gone = self.fake.feed.pop(3)
        self.assertEqual(self.sync(), 0)
        raw, st = self.my_raw(gone["id"])
        self.assertEqual((raw["text"], st), (gone["text"], "gone"))

    def test_content_404_keeps_the_full_post(self) -> None:
        original = self.raw_post(6)
        self.fake.posts[6]["dateModified"] += 100
        self.fake.content_404.add(6)
        self.assertEqual(self.sync(), 0)
        kept = self.raw_post(6)
        self.assertEqual(kept["blocks"], original["blocks"])
        self.assertNotEqual(kept.get("_source"), "timeline")
        self.assertEqual(kept["_site"]["state"], "unavailable")

    def test_edits_keep_history(self) -> None:
        self.fake.posts[2]["blocks"].append({"type": "text", "data": {"text": "<p>Дополнение</p>"}})
        old_mod = self.fake.posts[2]["dateModified"]
        self.fake.posts[2]["dateModified"] += 50
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(self.raw_post(2)["blocks"]), 2)
        hist = self.arch.raw_post_history(2, old_mod)
        self.assertTrue(hist.exists())
        self.assertEqual(len(read_json_gz(hist)["blocks"]), 1)

    def test_context_media_is_queued(self) -> None:
        # other people's pictures in the kept discussion branches are archived too (the app works offline)
        arch = Archive(self.lib / "tester", self.lib)
        s = Syncer(arch, None, no_media=True)
        s._queue_from_raw()
        n = arch.db.execute("SELECT COUNT(*) FROM media_use WHERE owner LIKE 'tc:%'").fetchone()[0]
        arch.close()
        self.assertEqual(n, 30)

    def test_guard_trip_is_an_exception_with_details(self) -> None:
        g = GuardTrip("posts-mass", "msg", {"lost": 3})
        self.assertEqual(g.as_meta()["details"], {"lost": 3})


if __name__ == "__main__":
    unittest.main()
