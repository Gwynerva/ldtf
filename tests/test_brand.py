"""LDTF icon files and the Windows autostart command (offline, nothing is written to the registry)."""

import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_icons  # noqa: E402
from dtf_backup import winintegration  # noqa: E402

BRAND = ROOT / "dtf_backup" / "assets" / "brand"


class IconsTest(unittest.TestCase):
    def test_png(self) -> None:
        data = make_icons.png(16, make_icons.render(16, make_icons.LIGHTS[""]))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", data[16:24]), (16, 16))

    def test_rendering(self) -> None:
        px = make_icons.render(32, make_icons.LIGHTS["-sync"])
        self.assertEqual(len(px), 32 * 32 * 4)
        corner, centre = px[0:4], px[(16 * 32 + 16) * 4:(16 * 32 + 16) * 4 + 4]
        self.assertEqual(corner[3], 0)                  # rounded tile: transparent corner
        self.assertEqual(centre[3], 255)

    def test_shipped_icons(self) -> None:
        for name in ("ldtf.ico", "ldtf-sync.ico", "ldtf-error.ico"):
            data = (BRAND / name).read_bytes()
            reserved, kind, count = struct.unpack("<HHH", data[:6])
            self.assertEqual((reserved, kind, count), (0, 1, len(make_icons.ICO_SIZES)), name)
            sizes = sorted((data[6 + 16 * i] or 256) for i in range(count))
            self.assertEqual(sizes, sorted(make_icons.ICO_SIZES))
        for n in ("ldtf.svg", "ldtf-32.png", "ldtf-128.png", "ldtf-180.png"):
            self.assertTrue((BRAND / n).stat().st_size > 100, n)


class WinIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows autostart")
    def test_autostart_command(self) -> None:
        cmd = winintegration.command(background=True)
        self.assertIn("pythonw", cmd.lower())
        self.assertIn("app", cmd)
        self.assertIn("--background", cmd)
        self.assertNotIn("--background", winintegration.command(background=False))


if __name__ == "__main__":
    unittest.main()
