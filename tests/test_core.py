"""Offline tests: python -m unittest discover -s tests"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.blocks import Ctx, Report, render_block  # noqa: E402
from dtf_backup.context import prune_context  # noqa: E402
from dtf_backup.http import HttpError, Response  # noqa: E402
from dtf_backup.media import Downloader, collect_media  # noqa: E402
from dtf_backup.normalize import Linker, MediaResolver, comment_text, unwrap_url  # noqa: E402

U1 = "11111111-2222-5333-8444-555555555555"
U2 = "aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"


def ctx(report: Report) -> Ctx:
    res = MediaResolver()
    return Ctx(res, Linker({4242}, lambda pid: f"/u/test/p/{pid}"), "/", "../../../", "post:1", "post 1", report, {4242})


class BlockFallbackTest(unittest.TestCase):
    def test_unknown_block_is_preserved(self):
        rep = Report()
        b = {"type": "futureBlock", "cover": False, "hidden": False, "anchor": "",
             "data": {"title": "Новое", "image": {"type": "image", "data": {"uuid": U1, "type": "jpg"}}}}
        h, md, norm = render_block(b, ctx(rep))
        self.assertIn("Неподдерживаемый блок", h)
        self.assertIn("<details", h)
        self.assertIn("futureBlock", h)
        self.assertIn(U1, h)                       # media preview inside the fallback card
        self.assertIs(norm["supported"], False)
        self.assertEqual(norm["raw"], b)           # original JSON kept as is
        self.assertIn("```json", md)
        self.assertIn("futureBlock", rep.buckets["unsupported"])
        self.assertIn(U1, [k for k, _, _ in collect_media(b)])

    def test_broken_known_block_does_not_crash(self):
        rep = Report()
        b = {"type": "media", "data": {"items": [{"image": {"type": "image", "data": {"uuid": U1}}}], "title": 5}}
        bad = {"type": "link", "data": {"link": {"data": {}}}}
        h, md, norm = render_block(bad, ctx(rep))
        self.assertIn("Не удалось отобразить", h)
        self.assertIs(norm["supported"], False)
        self.assertIn("link", rep.buckets["errors"])
        h2, _, n2 = render_block(b, ctx(rep))
        self.assertTrue(h2)

    def test_generic_block(self):
        rep = Report()
        b = {"type": "tweet", "data": {"tweet": {"data": {"tweet_data": {"text": "hello", "url": "https://x.com/a/1"}}}}}
        h, md, norm = render_block(b, ctx(rep))
        self.assertEqual(norm["supported"], "generic")
        self.assertIn("hello", h)
        self.assertIn("https://x.com/a/1", h)
        self.assertIn("tweet", rep.buckets["generic"])

    def test_spoiler_and_anchor(self):
        rep = Report()
        h, md, norm = render_block({"type": "text", "hidden": True, "anchor": "wtf", "data": {"text": "<p>секрет</p>"}},
                                   ctx(rep))
        self.assertIn('id="wtf"', h)
        self.assertIn("details class=\"spoiler\"", h)
        self.assertTrue(norm["spoiler"])

    def test_local_post_links(self):
        rep = Report()
        h, _, _ = render_block({"type": "text", "data": {"text":
                                '<p><a href="https://dtf.ru/petra/4242-slug">x</a> <a href="https://dtf.ru/u/4242-user">u</a> '
                                '<a href="https://api.dtf.ru/v2.8/redirect?to=https%3A%2F%2Ft.me%2Fa&postId=1">t</a></p>'}},
                               ctx(rep))
        self.assertIn('href="/u/test/p/4242"', h)
        self.assertIn('href="https://dtf.ru/u/4242-user"', h)
        self.assertIn('href="https://t.me/a"', h)


class TextTest(unittest.TestCase):
    def test_quotes_and_mentions(self):
        r = comment_text('> цитата\n&gt;вторая\nответ <mention id="7" nickname="n">Имя</mention> <1%')
        self.assertIn('<blockquote class="cq">цитата<br>вторая</blockquote>', r.html)
        self.assertIn('href="https://dtf.ru/id7"', r.html)
        self.assertIn("&lt;1%", r.html)
        self.assertIn("> цитата", r.md)

    def test_unwrap(self):
        self.assertEqual(unwrap_url("https://api.dtf.ru/v2.8/redirect?to=https%3A%2F%2Fya.ru&postId=5"), "https://ya.ru")


class ContextTest(unittest.TestCase):
    def test_prune(self):
        items = [{"id": 1, "replyTo": 0}, {"id": 2, "replyTo": 1}, {"id": 3, "replyTo": 2}, {"id": 4, "replyTo": 3},
                 {"id": 5, "replyTo": 1}, {"id": 6, "replyTo": 0}, {"id": 7, "replyTo": 4}]
        kept, missing = prune_context(items, [3, 99])
        self.assertEqual([c["id"] for c in kept], [1, 2, 3, 4, 7])   # ancestors + me + replies; siblings dropped
        self.assertEqual(missing, [99])


class FakeClient:
    """Serves bytes per URL; counts full downloads."""
    def __init__(self, files):
        self.files = files
        self.downloads = 0

    def _body(self, url):
        for k, v in self.files.items():
            if k in url:
                return v
        raise HttpError(404, url)

    def download(self, url, dest, byte_range=None):
        body = self._body(url)
        self.downloads += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return Response(200, url, {"content-type": "image/png", "content-length": str(len(body))},
                        sha256=hashlib.sha256(body).hexdigest(), size=len(body))

    def fetch_range(self, url, byte_range):
        body = self._body(url)
        if byte_range.startswith("bytes=-"):
            part = body[-int(byte_range[7:]):]
        else:
            a, b = byte_range[6:].split("-")
            part = body[int(a):int(b) + 1]
        return Response(206, url, {"content-range": f"bytes 0-0/{len(body)}"}, body=part)


class DedupTest(unittest.TestCase):
    def test_same_content_two_uuids(self):
        content = b"PNG" + b"x" * 200_000
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient({U1: content, U2: content})
            dl = Downloader(client, Path(d))
            r1 = dl.fetch(U1, "png", [])
            self.assertEqual(r1["via"], "download")
            # no probe candidates: full download, then sha256 dedup
            r2 = dl.fetch(U2, "png", [])
            self.assertEqual(r2["via"], "dedup")
            self.assertEqual(r1["sha256"], r2["sha256"])
            files = [p for p in Path(d).rglob("*") if p.is_file() and ".tmp" not in p.parts]
            self.assertEqual(len(files), 1)
            # with a signature match the probe avoids the full download
            before = client.downloads
            r3 = dl.fetch(U2, "png", [{"sha256": r1["sha256"], "size": r1["size"], "path": r1["path"]}])
            self.assertEqual(r3["via"], "probe")
            self.assertEqual(client.downloads, before)

    def test_missing(self):
        with tempfile.TemporaryDirectory() as d:
            dl = Downloader(FakeClient({}), Path(d))
            self.assertEqual(dl.fetch(U1, "png", [])["status"], "missing")


if __name__ == "__main__":
    unittest.main()


class ReactionsTest(unittest.TestCase):
    def test_known_and_unknown(self):
        from dtf_backup.reactions import Reactions, reaction_pairs
        rep = Report()
        rx = Reactions({"reactions": [{"id": 1, "type": "free", "staticUuid": U1, "animatedUuid": None}]},
                       MediaResolver(), rep)
        pairs = reaction_pairs({"reactions": {"counters": [{"id": 1, "count": 3}, {"id": 27, "count": 5},
                                                           {"id": 2, "count": 0}, {"id": "x", "count": "bad"}]}})
        self.assertEqual(pairs, [(27, 5), (1, 3)])
        h = rx.html(pairs, "../../", "post 1")
        self.assertIn(f"https://leonardo.osnova.io/{U1}/", h)
        self.assertIn("Неизвестная реакция #27", h)
        self.assertIn("27", rep.buckets["unknownReactions"])
        self.assertEqual(rx.norm(pairs)["items"][0], {"id": 27, "count": 5, "polarity": "positive", "unknown": True})
        neg = Reactions({"reactions": []}, MediaResolver(), rep, {"negative": [25]})
        self.assertEqual(neg.split([(25, 2), (1, 5)]), (5, 2))
        self.assertIn("▼ 2", neg.score_html([(25, 2), (1, 5)], 7))

    def test_library_config_and_catalog(self):
        """One dislike list for the library: per-archive lists of LDTF 1.3 move to it once (the newest wins when they
        differ); the catalog joins what every archive saved."""
        import gzip
        import json
        import os
        import tempfile
        from pathlib import Path
        from dtf_backup.reactions import CONFIG_NAME, Reactions, config_path, library_assets, load_config, save_negative
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            for nick, neg, rx, age in (("a", [5, 7], [{"id": 1}, {"id": 2, "retired": True}], 100),
                                       ("b", [25], [{"id": 2}, {"id": 3}], 10)):
                (lib / nick / "raw").mkdir(parents=True)
                (lib / nick / "raw" / "profile.json.gz").write_bytes(gzip.compress(b'{"id": 1}'))
                (lib / nick / "raw" / "assets.json.gz").write_bytes(gzip.compress(json.dumps({"reactions": rx}).encode()))
                p = lib / nick / CONFIG_NAME
                p.write_text(json.dumps({"negative": neg}), encoding="utf-8")
                t = os.path.getmtime(p) - age
                os.utime(p, (t, t))
            self.assertEqual(load_config(lib)["negative"], [25])           # b's list is the newest
            self.assertTrue(config_path(lib).exists())
            self.assertFalse((lib / "a" / CONFIG_NAME).exists() or (lib / "b" / CONFIG_NAME).exists())
            self.assertEqual(save_negative(lib, ["7", "x", 7, " 3 "]), [7, 3])
            self.assertEqual(load_config(lib)["negative"], [7, 3])
            cat = {str(x["id"]): x for x in library_assets(lib)["reactions"]}
            self.assertEqual(sorted(cat), ["1", "2", "3"])
            self.assertFalse(cat["2"].get("retired"))                        # b still has it
            rx = Reactions.for_library(lib, MediaResolver(), None)
            self.assertEqual(rx.split([(7, 2), (1, 5)]), (5, 2))
