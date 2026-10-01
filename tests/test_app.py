"""Offline tests of the app on a small fixture archive: view.sqlite build, month grouping, exports,
HTTP routes, CSRF/Origin/Host protection, Range responses, job queue, shared media GC."""

import gzip
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.render import render  # noqa: E402
from dtf_backup.settings import save_settings  # noqa: E402
from dtf_backup.state import Archive, gc_media, open_store  # noqa: E402

UID = 777
OWN_POST = 5000001
FOREIGN = 6000001
IMG = "11111111-2222-5333-8444-555555555555"


def gz(path: Path, obj, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(json.dumps(o, ensure_ascii=False) for o in obj) if lines else json.dumps(obj, ensure_ascii=False)
    path.write_bytes(gzip.compress(data.encode("utf-8")))


def author(uid: int, name: str) -> dict:
    return {"id": uid, "name": name, "nickname": name.lower(), "uri": f"/{name.lower()}",
            "avatar": {"type": "image", "data": {"uuid": IMG}}}


def comment(cid: int, parent: int, level: int, uid: int, name: str, date: int, text: str, entry: int,
            title: str = "Пост") -> dict:
    return {"id": cid, "replyTo": parent, "level": level, "author": author(uid, name), "date": date, "text": text,
            "media": [], "likes": {"counterLikes": 1}, "reactions": {"counters": [{"id": 1, "count": 1}]},
            "replyCount": 0, "entry": {"id": entry, "title": title, "subsiteId": 1, "subsiteName": "Сабсайт"}}


def make_archive(library: Path, nick: str = "tester") -> Archive:
    root = library / nick
    t = 1790000000  # 2026-09
    gz(root / "raw" / "profile.json.gz", {"id": UID, "name": "Тестер", "nickname": nick, "uri": f"/{nick}",
                                           "url": f"https://dtf.ru/{nick}", "created": 1600000000,
                                           "avatar": {"type": "image", "data": {"uuid": IMG}}})
    gz(root / "raw" / "assets.json.gz", {"reactions": [{"id": 1, "type": "free", "staticUuid": IMG}], "badges": []})
    gz(root / "raw" / "posts" / f"{OWN_POST}.json.gz", {
        "id": OWN_POST, "date": t, "title": "Мой пост про катану", "url": f"https://dtf.ru/{nick}/{OWN_POST}-katana",
        "counters": {"comments": 2}, "reactions": {"counters": [{"id": 1, "count": 3}]},
        "blocks": [{"type": "text", "data": {"text": "<p>Текст про <b>катану</b></p>"}},
                   {"type": "media", "data": {"items": [{"image": {"type": "image", "data": {"uuid": IMG, "type": "jpg"}}},
                                                        {"image": {"type": "image", "data": {"uuid": IMG, "type": "jpg"}}}]}},
                   {"type": "futureBlock", "data": {"x": 1}}]})
    gz(root / "raw" / "post-trees" / f"{OWN_POST}.json.gz", {"items": [
        comment(11, 0, 0, 42, "Гость", t + 10, "Первый!", OWN_POST),
        comment(12, 11, 1, UID, "Тестер", t + 20, "Спасибо", OWN_POST)]})
    # the user's feed: one comment under the own post, a dialog of two replies in a foreign thread
    feed = [comment(12, 11, 1, UID, "Тестер", t + 20, "Спасибо", OWN_POST, "Мой пост про катану"),
            comment(22, 21, 1, UID, "Тестер", t + 30, "> цитата\nОтвет", FOREIGN, "Чужой пост"),
            comment(24, 23, 3, UID, "Тестер", t + 50, "Ещё ответ", FOREIGN, "Чужой пост")]
    gz(root / "raw" / "my-comments" / "2026.jsonl.gz", feed, lines=True)
    gz(root / "raw" / "threads" / f"{FOREIGN}.json.gz", {"entryId": FOREIGN, "items": [
        comment(21, 0, 0, 43, "Другой", t + 25, "Корень ветки: косплей и ещё раз о косплее", FOREIGN),
        comment(22, 21, 1, UID, "Тестер", t + 30, "> цитата\nОтвет", FOREIGN),
        comment(23, 22, 2, 43, "Другой", t + 40, "Возражение: катаной так не рубят", FOREIGN),
        comment(24, 23, 3, UID, "Тестер", t + 50, "Ещё ответ", FOREIGN)]})
    arch = Archive(root, library)
    arch.set_meta("user_id", UID)
    arch.queue_media([(IMG, None, "jpg")], f"post:{OWN_POST}")
    arch.commit()
    # the file itself in the shared store
    content = b"\xff\xd8JPEG" + b"x" * 5000
    sha = hashlib.sha256(content).hexdigest()
    f = library / "media" / sha[:2] / f"{sha}.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    arch.db.execute("INSERT OR IGNORE INTO store.blob VALUES (?,?,?,?,?,?)", (sha, len(content), "jpg", "image/jpeg", None,
                                                                          f"{sha[:2]}/{sha}.jpg"))
    arch.db.execute("UPDATE store.media_ref SET status='done', sha256=? WHERE key=?", (sha, IMG))
    arch.commit()
    return arch


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.library = Path(cls.tmp.name) / "archive"
        cls.arch = make_archive(cls.library)
        cls.report = render(cls.arch)
        from dtf_backup.web.server import App, Handler
        cls.app = App(cls.library, 0)
        Handler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.app.port = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.invalidate()
        cls.arch.close()
        import gc
        gc.collect()
        cls.tmp.cleanup()

    def get(self, path: str, headers: dict | None = None, redirect: bool = True) -> tuple[int, str, dict]:
        path = urllib.parse.quote(path, safe="/?=&#%:")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def post(self, path: str, data: dict, headers: dict | None = None) -> int:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=headers or {})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def post_json(self, path: str, data: dict) -> tuple[int, dict]:
        """A settings control saving itself (app.js): only that field is sent, the answer is JSON."""
        body = urllib.parse.urlencode(data, doseq=True).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # ---------------------------------------------------------------- build + exports
    def test_view_and_exports(self) -> None:
        import sqlite3
        db = sqlite3.connect(self.arch.view_path)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM comments WHERE feed=1").fetchone()[0], 3)
        # the two replies in one foreign thread form ONE group; the own-post comment is another group
        groups = db.execute("SELECT entry_id, root_id, mine FROM month_groups ORDER BY pos").fetchall()
        self.assertEqual(len(groups), 2)
        foreign = [g for g in groups if g[0] == FOREIGN][0]
        self.assertEqual(foreign[1], 21)
        self.assertEqual(sorted(json.loads(foreign[2])), [22, 24])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fts WHERE fts MATCH '\"катан\"*'").fetchone()[0] >= 1, True)
        db.close()
        rows = [json.loads(x) for x in (self.arch.root / "data" / "comments.jsonl").read_text(encoding="utf-8").splitlines()]
        r24 = [r for r in rows if r["id"] == 24][0]
        self.assertEqual(r24["ancestorIds"], [21, 22, 23])
        self.assertEqual(r24["contextStatus"], "ok")
        md = (self.arch.root / "md" / "comments" / "2026-09.md").read_text(encoding="utf-8")
        self.assertEqual(md.count("## "), 2)                 # one section per thread group
        self.assertIn("(автор архива)", md)
        self.assertIn("futureBlock", self.report["unsupported"])
        self.assertFalse((self.arch.root / "site").exists())

    # ---------------------------------------------------------------- pages
    def test_pages(self) -> None:
        for path in ("/u/tester/", "/u/tester/posts", f"/u/tester/p/{OWN_POST}", "/u/tester/comments",
                     "/u/tester/c/2026-09", "/u/tester/search?q=катана", "/u/tester/reactions", "/u/tester/sync",
                     "/u/tester/settings", "/archives", "/add", "/diagnostics"):
            code, body, _ = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertNotIn("<h1>Ошибка", body, path)
        code, body, _ = self.get(f"/u/tester/p/{OWN_POST}")
        self.assertIn("Неподдерживаемый блок", body)
        self.assertIn('class="b-media b-gallery pswp-gallery"', body)
        code, body, _ = self.get("/u/tester/c/2026-09")
        self.assertEqual(body.count('class="mc card'), 2)
        self.assertIn('id="c24"', body)
        self.assertIn('<blockquote class="cq">цитата</blockquote>', body)
        code, body, _ = self.get("/u/tester/search?q=катана")
        self.assertIn("<mark>катану</mark>", body)
        self.assertIn("<mark>катаной</mark>", body)
        self.assertIn("sh-ctx", body)                          # parent comment shown as context
        code, body, _ = self.get("/u/tester/search?q=rfnfyf")
        self.assertIn("Искать как написано", body)
        code, body, _ = self.get("/u/tester/search?q=rfnfyf&exact=1")
        self.assertIn("Найдено: 0", body)
        for path in ("/u/tester/p/1", "/u/nobody/", "/nope"):
            self.assertEqual(self.get(path)[0], 404, path)
        code, _, h = self.get("/u/tester/go/c/23", redirect=False)
        self.assertEqual(code, 302)
        self.assertIn("/u/tester/c/2026-09#c23", h.get("Location", ""))

    def test_shell(self) -> None:
        import re
        ck = {"Cookie": "last=tester"}

        def header(path: str) -> str:
            code, body, _ = self.get(path, ck)
            self.assertIn(code, (200, 404), path)
            h = re.search(r'<header class="appbar">.*?</header>', body, re.S)
            self.assertIsNotNone(h, path)
            # the active marks differ between pages; everything else must be identical
            return re.sub(r' (on|aria-current=page)(?=[">\s])|<svg[^>]*>.*?</svg>', "", h.group(0))

        pages = ["/u/tester/", "/u/tester/c/2026-09", "/u/tester/search?q=катана", "/u/tester/sync", "/archives",
                 "/add", "/diagnostics", "/nope"]
        heads = {p: header(p) for p in pages}
        for p in pages:
            self.assertEqual(heads[p], heads["/u/tester/"], f"app bar differs on {p}")
        h = heads["/archives"]
        self.assertIn('href="/u/tester/posts"', h)          # app pages keep the archive's navigation
        self.assertNotIn("Выберите архив", h)
        self.assertRegex(h, r"\d+ пост\w* · \d+ комментари\w*")  # post and comment counts in the switcher

    def test_app_settings_and_schedule(self) -> None:
        import re
        from unittest import mock
        code, body, _ = self.get("/app")
        self.assertEqual(code, 200)
        for knob in ("Одновременно архивов", "Запросов к DTF", "Параллельных"):   # the app tunes the network itself
            self.assertNotIn(knob, body)
        self.assertNotIn("savebar", body)                              # settings save themselves, no "Сохранить"
        self.assertIn('data-autosave autocomplete="off"', body)
        token = re.search(r'name="_csrf" value="([^"]+)"', body).group(1)
        with mock.patch("dtf_backup.winintegration.available", lambda: False):   # never touch the real registry
            self.assertEqual(self.post("/app", {"_csrf": token, "autosync": "1", "notify": "1"}), 303)
            code, body, _ = self.get("/app")
        self.assertIn("Сохранено", body)                               # a form sent without JS: the outcome, once
        self.assertNotIn("Сохранено", self.get("/app")[1])
        code, body, _ = self.get("/u/tester/settings")
        self.assertIn("Опасная зона", body)
        self.assertIn('name="schedule"', body)
        self.assertNotIn("savebar", body)
        self.assertNotIn("refresh_days", body)                         # a tuning knob: settings.json / CLI only
        self.assertLess(body.index('value="daily"'), body.index('value="interval"'))   # the default comes first
        self.assertEqual(self.post("/u/tester/settings", {"_csrf": token, "schedule": "daily", "schedule_time": "05:30",
                                                          "schedule_hours": "12", "media": "1", "scope": "all"}), 303)
        self.assertEqual(self.arch.settings()["schedule"], "daily")
        code, body, _ = self.get("/u/tester/settings")
        self.assertIn("Следующая автосинхронизация", body)
        self.assertIn("Сохранено", body)

    def test_autosave_app_settings(self) -> None:
        """A page saves one control at a time: a switch the page shows off never turns other settings off, and the
        autostart entry is written only by its own switch."""
        from unittest import mock
        from dtf_backup.appsettings import load_app_settings
        calls: list = []
        self.app.save_settings({"autosync": True, "notify": True, "open_browser": True})
        try:
            with mock.patch("dtf_backup.winintegration.available", lambda: True), \
                    mock.patch("dtf_backup.winintegration.set_autostart", lambda on, **k: calls.append(on) or on), \
                    mock.patch("dtf_backup.winintegration.autostart_state", lambda: "on" if calls and calls[-1] else "off"):
                code, j = self.post_json("/app", {"_csrf": self.app.csrf, "notify": "0"})
                self.assertEqual(code, 200)
                self.assertEqual((j["values"]["notify"], j["values"]["autosync"], j["values"]["open_browser"]),
                                 (False, True, True))
                self.assertEqual(calls, [])                     # no autostart field: the registry is not touched
                self.assertEqual(load_app_settings(self.library)["notify"], False)
                code, j = self.post_json("/app", {"_csrf": self.app.csrf, "autostart": "1"})
                self.assertEqual((code, calls, j["values"]["autostart"], j["note"]), (200, [True], True, "Автозапуск включён"))
                self.assertIn("autostart", j["regions"])
                self.assertEqual(load_app_settings(self.library)["notify"], False)   # the other switches stay
                code, j = self.post_json("/app", {"_csrf": "from-another-life", "autosync": "0"})
                self.assertEqual((code, j.get("stale")), (403, True))                  # an error the page can show
                self.assertTrue(load_app_settings(self.library)["autosync"])
        finally:
            self.app.save_settings({"notify": True})

    def test_autosave_archive_settings(self) -> None:
        settings = dict(self.arch.settings())
        try:
            code, j = self.post_json("/u/tester/settings", {"_csrf": self.app.csrf, "schedule_hours": "999"})
            self.assertEqual((code, j["values"]["schedule_hours"]), (200, 168))   # corrected: the page shows it
            self.assertEqual(self.arch.settings()["schedule"], settings["schedule"])   # the rest stays
            self.assertIn("sched", j["regions"])
            # switching to posts only: nothing is saved before the user confirms in the card
            code, j = self.post_json("/u/tester/settings", {"_csrf": self.app.csrf, "scope": "posts"})
            self.assertEqual(code, 200)
            self.assertIn("Перейти на «Только посты»", j["confirm"])
            self.assertIn('name="confirm_drop"', j["confirm"])
            self.assertEqual(self.arch.settings()["scope"], "all")
        finally:
            save_settings(self.arch.settings_path, settings)
            self.app.invalidate()

    def test_autosave_reactions(self) -> None:
        from dtf_backup.reactions import CONFIG_NAME
        cfg = self.arch.root / CONFIG_NAME
        before = cfg.read_bytes() if cfg.exists() else None
        try:
            code, body, _ = self.get("/u/tester/reactions")
            self.assertIn("data-autosave", body)
            self.assertNotIn("savebar", body)
            code, j = self.post_json("/u/tester/reactions", {"_csrf": self.app.csrf, "neg": ["", "1"]})
            self.assertEqual((code, j["values"]["neg"]), (200, ["1"]))
            code, j = self.post_json("/u/tester/reactions", {"_csrf": self.app.csrf, "neg": [""]})   # none checked
            self.assertEqual((code, j["values"]["neg"]), (200, []))
        finally:
            if before is None:
                cfg.unlink(missing_ok=True)
            else:
                cfg.write_bytes(before)
            self.app.invalidate()

    def test_form_token_survives_restart(self) -> None:
        """A page opened before LDTF restarted still saves: the token is kept with the library."""
        from dtf_backup.web.server import form_secret
        self.assertEqual(form_secret(self.library), self.app.csrf)
        self.assertTrue((self.library / ".state" / "app.secret").exists())

    def test_app_settings_form_has_no_buttons(self) -> None:
        """Settings save themselves: the settings form has no submit buttons (Enter never sends it), the shortcut
        buttons belong to a form of their own; the page tells when Windows itself skips the autostart entry."""
        import re
        from unittest import mock
        with mock.patch("dtf_backup.winintegration.available", lambda: True), \
                mock.patch("dtf_backup.winintegration.autostart_state", lambda: "off"):
            _, body, _ = self.get("/app")
        form = re.search(r'<form method="post" action="/app"[^>]*>(.*?)</form>', body, re.S).group(1)
        buttons = re.findall(r"<button[^>]*>", form)
        self.assertTrue(buttons)
        self.assertTrue(all('form="shortcuts"' in b for b in buttons), buttons)
        self.assertIn('<form method="post" action="/app/shortcut" id="shortcuts">', body)
        self.assertNotIn("Автозагрузка", body)
        with mock.patch("dtf_backup.winintegration.available", lambda: True), \
                mock.patch("dtf_backup.winintegration.autostart_state", lambda: "disabled"):
            _, body, _ = self.get("/app")
        self.assertIn("Автозагрузка", body)
        self.assertRegex(body, r'name="autostart" value="1" checked')

    def test_guard_ui(self) -> None:
        from unittest import mock
        from dtf_backup.guard import GuardTrip
        g = GuardTrip("posts-mass", "С прошлой синхронизации на DTF пропали 4 поста из 8 (порог — 3). Архив не изменён.",
                      {"lost": 4, "samples": [{"id": OWN_POST, "title": "Пропавший пост", "url": "https://dtf.ru/1"}]})
        self.arch.set_meta("guard", g.as_meta())
        self.arch.commit()
        self.app.invalidate()
        settings = dict(self.arch.settings())
        try:
            _, body, _ = self.get("/u/tester/posts")
            self.assertIn("guard-banner", body)
            self.assertIn("Синхронизация остановлена", body)
            _, body, _ = self.get("/u/tester/sync")
            self.assertNotIn("guard-banner", body)            # the tab shows the full card instead
            self.assertIn("guard-card", body)
            self.assertIn("Пропавший пост", body)
            self.assertIn("…и ещё 3", body)
            self.assertIn("Продолжить и сохранить удалённое", body)
            _, body, _ = self.get("/archives")
            self.assertIn("нужна проверка", body)
            self.assertIsNone(self.app.next_run("tester"))    # no scheduled retries while stopped
            self.assertIn(("tester", None), self.app.scheduler.due_list())
            # a sync while stopped ends as "blocked" without asking DTF
            j = self.app.jobs.submit("tester", "sync")
            for _ in range(120):
                if j.state not in ("queued", "running"):
                    break
                time.sleep(0.25)
            self.assertEqual(j.state, "blocked", j.snapshot())
            self.assertIn("пропали 4 поста", j.error)
            calls = []
            with mock.patch.object(self.app.jobs, "submit", lambda *a, **k: calls.append((a, k))):
                self.assertIn(self.post("/u/tester/guard", {"_csrf": self.app.csrf, "action": "accept"}), (302, 303))
                self.assertEqual(calls[-1][1].get("accept"), True)
                self.assertIn(self.post("/u/tester/guard", {"_csrf": self.app.csrf, "action": "retry"}), (302, 303))
                self.assertIsNone(self.arch.get_meta("guard"))
                self.assertEqual(len(calls), 2)
        finally:
            self.arch.set_meta("guard", None)
            self.arch.commit()
            save_settings(self.arch.settings_path, settings)
            self.app.invalidate()

    def test_broken_view_does_not_break_the_app(self) -> None:
        bad = self.library / "broken"
        (bad / ".state").mkdir(parents=True)
        (bad / ".state" / "state.sqlite").write_bytes(b"")
        (bad / ".state" / "view.sqlite").write_bytes(b"")          # e.g. created empty by some other tool
        self.app.invalidate()
        try:
            for p in ("/archives", "/u/tester/", "/u/broken/sync"):
                self.assertEqual(self.get(p)[0], 200, p)
        finally:
            import shutil
            shutil.rmtree(bad, ignore_errors=True)
            self.app.invalidate()

    def test_add_deleted_account(self) -> None:
        import json as _json
        from dtf_backup.web.app_pages import add_page
        sample = _json.loads((Path(__file__).parent / "fixtures" / "guard" / "api_samples.json").read_text(encoding="utf-8"))
        page = add_page(self.app, "123711", preview=sample["profile_deleted"])
        self.assertIn("архивировать нечего", page)
        self.assertNotIn("Создать архив", page)

    def test_job_api_needs_token(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/jobs", data=b'{"nick": "tester"}',
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/jobs", data=b'{"nick": "../x"}',
                                     headers={"X-LDTF-Token": self.app.token})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)

    def test_no_emoji_chrome(self) -> None:
        for p in ["/u/tester/", "/u/tester/posts", f"/u/tester/p/{OWN_POST}", "/u/tester/comments", "/u/tester/c/2026-09",
                  "/u/tester/search?q=катана", "/u/tester/reactions", "/u/tester/sync", "/u/tester/settings", "/archives",
                  "/add", "/diagnostics"]:
            _, body, _ = self.get(p)
            for ch in "📝📄💬🔖🔁⟳■✓✗↗▸▾↳📎📊📁⚠♥":
                self.assertNotIn(ch, body, f"{ch} on {p}")

    def test_search_engine(self) -> None:
        import sqlite3
        from dtf_backup.search.engine import search
        db = sqlite3.connect(self.arch.view_path)

        def refs(q: str, **kw) -> set:
            r = search(db, q, **kw)
            self.assertIsNone(r.error, q)
            return {(h.kind, h.ref) for h in r.hits}

        # word forms: post has "катану", comment 23 has "катаной"
        self.assertEqual(refs("катана"), {("p", OWN_POST), ("o", 23)})
        self.assertEqual(refs("кат*"), {("p", OWN_POST), ("o", 23)})
        # typo with a Latin "c" inside a Russian word, and the wrong keyboard layout
        r = search(db, "каcплей")
        self.assertTrue(r.notes, "typo must be corrected")
        self.assertIn(("o", 21), {(h.kind, h.ref) for h in r.hits})
        r = search(db, "rfnfyf")
        self.assertIn("раскладка", " ".join(r.notes))
        self.assertEqual({(h.kind, h.ref) for h in r.hits}, {("p", OWN_POST), ("o", 23)})
        self.assertEqual(search(db, "rfnfyf", exact=True).total, 0)
        # phrases, exclusion, OR
        self.assertEqual(refs('"текст про катану"'), {("p", OWN_POST)})
        self.assertEqual(refs('"про текст"'), set())
        self.assertEqual(refs("катана -рубят"), {("p", OWN_POST)})
        self.assertEqual(refs("косплей OR рубят"), {("o", 21), ("o", 23)})
        self.assertEqual(refs("катана", kind="p"), {("p", OWN_POST)})
        self.assertIsNotNone(search(db, "-катана").error)
        db.close()

    def test_media_range(self) -> None:
        idx = json.loads((self.arch.root / "data" / "media.jsonl").read_text(encoding="utf-8").splitlines()[0])
        code, _, h = self.get("/" + idx["path"], {"Range": "bytes=0-5"})
        self.assertEqual(code, 206)
        self.assertTrue(h.get("Content-Range", "").startswith("bytes 0-5/"))
        self.assertEqual(self.get("/media/../.state/media.sqlite")[0], 404)

    # ---------------------------------------------------------------- security
    def test_security(self) -> None:
        self.assertEqual(self.get("/archives", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post("/u/tester/render", {}), 403)                                # no token
        self.assertEqual(self.post("/u/tester/render", {"_csrf": self.app.csrf},
                                   {"Origin": "https://evil.example"}), 403)                     # foreign site
        self.assertEqual(self.post("/u/tester/settings", {"_csrf": self.app.csrf, "workers": "3",
                                                          "auto_sync_hours": "0"}), 303)
        self.assertNotIn("workers", self.arch.settings())
        self.assertEqual(self.post("/u/tester/delete", {"_csrf": self.app.csrf, "confirm": "wrong"}), 200)
        self.assertTrue(self.arch.root.exists())

    # ---------------------------------------------------------------- jobs
    def test_jobs_queue_and_cancel(self) -> None:
        jm = self.app.jobs
        with jm.cond:   # the queue holds still while we look (the workers wait for this lock)
            j1 = jm.submit("tester", "render")
            self.assertIs(jm.submit("tester", "render"), j1)       # no duplicates while queued/running
            j2 = jm.submit("tester", "sync")
            self.assertNotIn(j1, jm.queue)                        # a sync builds the archive: the waiting rebuild goes
            self.assertIs(jm.submit("tester", "render"), j2)       # and a rebuild asked meanwhile is that sync
            self.assertTrue(jm.cancel(j2.id))
        self.assertEqual(j2.state, "cancelled")
        j3 = jm.submit("tester", "render")
        for _ in range(240):
            if j3.state not in ("queued", "running"):
                break
            time.sleep(0.25)
        self.assertEqual(j3.state, "done", j3.snapshot())
        self.assertEqual([s["key"] for s in j3.snapshot()["stages"]], ["build"])


class GcTest(unittest.TestCase):
    def test_shared_blob_survives(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "archive"
            a = make_archive(lib, "one")
            b = make_archive(lib, "two")          # same image: one shared blob
            store = open_store(lib)
            self.assertEqual(store.execute("SELECT COUNT(*) FROM store.blob").fetchone()[0], 1)
            store.close()
            a.close()
            b.close()
            import shutil
            shutil.rmtree(lib / "one")
            self.assertEqual(gc_media(lib), {"status": "done", "files": 0, "bytes": 0, "reason": ""})   # used by "two"
            shutil.rmtree(lib / "two")
            self.assertEqual(gc_media(lib)["files"], 1)
            self.assertFalse(any((lib / "media").rglob("*.jpg")))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: ANN002, ANN003
        return None


if __name__ == "__main__":
    unittest.main()
