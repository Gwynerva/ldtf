"""How files are shown: from the archive; from DTF when not downloaded (marked, with a placeholder on failure);
never requested when DTF already reported them deleted."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.blocks import Ctx, Report, media_html, render_block  # noqa: E402
from dtf_backup.media import avatar_key  # noqa: E402
from dtf_backup.normalize import Linker, MediaResolver, media_info  # noqa: E402
from dtf_backup.web.ui import avatar, avatar_src  # noqa: E402

LOCAL = "11111111-2222-5333-8444-555555555555"
REMOTE = "aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"
GONE = "99999999-8888-5777-8666-555555555555"
INDEX = {LOCAL: {"path": "media/11/x.jpg", "mime": "image/jpeg", "size": 10}, GONE: {"missing": True}}


def ctx() -> Ctx:
    return Ctx(MediaResolver(dict(INDEX)), Linker(set(), lambda pid: f"/u/t/p/{pid}"), "/", "../", "post:1", "post 1",
               Report())


def img(uuid: str) -> dict:
    return {"type": "image", "data": {"uuid": uuid, "type": "jpg", "width": 800, "height": 600}}


class MediaViewTest(unittest.TestCase):
    def html(self, uuid: str, link: bool = True) -> str:
        c = ctx()
        return media_html(media_info(img(uuid), c.resolver), c, link=link)

    def test_local_file(self) -> None:
        h = self.html(LOCAL)
        self.assertIn('src="/media/11/x.jpg"', h)
        self.assertNotIn("data-remote", h)
        self.assertNotIn("mstub", h)

    def test_not_downloaded_is_shown_from_dtf(self) -> None:
        h = self.html(REMOTE)
        self.assertIn(f'src="https://leonardo.osnova.io/{REMOTE}/"', h)
        self.assertIn('data-remote="image"', h)
        self.assertIn('width="800" height="600"', h)   # the placeholder keeps the size if the load fails
        self.assertNotIn("data-failed", h)

    def test_gone_is_never_requested(self) -> None:
        h = self.html(GONE)
        self.assertNotIn(" src=", h)                  # no request: DTF already answered 404
        self.assertIn(f'data-src="https://leonardo.osnova.io/{GONE}/"', h)
        self.assertIn('data-failed="gone"', h)
        self.assertIn('class="mstub"', h)
        self.assertIn("Удалено с DTF", h)
        self.assertIn("--w:800;--h:600", h)
        self.assertIn('class="pswp-item"', h)         # stays a gallery item: lightbox indexes keep matching
        self.assertNotIn("<button", h)                # inside the lightbox link the click itself retries

    def test_inside_another_link_nothing_interactive(self) -> None:
        h = self.html(GONE, link=False)
        self.assertNotIn("<a", h)
        self.assertNotIn("<button", h)
        c = ctx()
        video = media_html(dict(media_info(img(GONE), c.resolver), kind="video", hasAudio=True), c)
        self.assertIn("mstub-retry", video)          # a standalone player gets a retry button

    def test_gallery_with_a_gone_file(self) -> None:
        c = ctx()
        b = {"type": "media", "data": {"items": [{"image": img(LOCAL)}, {"image": img(GONE)}, {"image": img(REMOTE)}]}}
        h, _, norm = render_block(b, c)
        tiles = re.findall(r'class="g-tile[^"]*" data-i="(\d)"', h)
        slides = re.findall(r'class="g-slide[^"]*" data-i="(\d)"', h)
        self.assertEqual(tiles, ["0", "1", "2"])
        self.assertEqual(slides, ["0", "1", "2"])
        self.assertEqual(h.count('class="pswp-item"'), 3)   # one lightbox item per slide, the gone one too
        self.assertTrue(norm["items"][1]["media"]["gone"])

    def test_avatars(self) -> None:
        r = MediaResolver({**INDEX, avatar_key(GONE): {"missing": True}})
        remote = avatar_src(r, {"data": {"uuid": REMOTE}})
        self.assertIn('data-remote="avatar"', avatar(remote))
        self.assertIsNone(avatar_src(r, {"data": {"uuid": GONE}}))
        self.assertIn("av0", avatar(None))


if __name__ == "__main__":
    unittest.main()
