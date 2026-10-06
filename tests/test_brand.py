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
        from unittest import mock
        with mock.patch.object(winintegration, "launcher", lambda root=None: None):   # a git clone
            cmd = winintegration.command(background=True)
            self.assertIn("pythonw", cmd.lower())
            self.assertIn("app", cmd)
            self.assertIn("--background", cmd)
            self.assertNotIn("--background", winintegration.command(background=False))
        exe = Path(r"C:\Program Files\LDTF\LDTF.exe")
        with mock.patch.object(winintegration, "launcher", lambda root=None: exe):     # a release: LDTF.exe
            self.assertEqual(winintegration.command(background=True), f'"{exe}" --background')
            self.assertEqual(winintegration.command(background=False), f'"{exe}"')
        # autostart of LDTF 1.3 (pythonw.exe in this folder) follows the new launcher
        old = rf'"{winintegration.APP_ROOT}\runtime\pythonw.exe" -X utf8 -m dtf_backup app --background'
        new = f'"{winintegration.APP_ROOT}\\LDTF.exe" --background'
        self.assertTrue(winintegration.needs_repoint(old, new, lambda p: True))

    def test_autostart_state(self) -> None:
        w = winintegration
        mine = r'"C:\LDTF\runtime\pythonw.exe" -X utf8 -m dtf_backup app --background'
        self.assertEqual(w.command_exe(mine), r"C:\LDTF\runtime\pythonw.exe")
        self.assertEqual(w.command_exe(r"D:\x\pythonw.exe -m y"), r"D:\x\pythonw.exe")
        self.assertEqual(w.state_of(None, None, mine), "off")
        self.assertEqual(w.state_of(mine, None, mine), "on")
        self.assertEqual(w.state_of(mine, bytes([2]) + bytes(11), mine), "on")
        for flag in (1, 3, 7):   # turned off in Windows' startup apps: the entry stays, Windows skips it
            self.assertEqual(w.state_of(mine, bytes([flag]) + bytes(11), mine), "disabled")
        self.assertEqual(w.state_of(r"E:\other\pythonw.exe -m dtf_backup app", None, mine), "elsewhere")

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_autostart_follows_only_a_moved_app(self) -> None:
        w, root = winintegration, Path(r"C:\LDTF")
        mine = r"C:\LDTF\runtime\pythonw.exe -X utf8 -m dtf_backup app --background"
        exists = lambda p: "missing" not in p   # noqa: E731
        self.assertFalse(w.needs_repoint(None, mine, exists, root))                        # autostart is off
        self.assertFalse(w.needs_repoint(mine, mine, exists, root))
        self.assertTrue(w.needs_repoint(r"D:\missing\pythonw.exe -m dtf_backup app", mine, exists, root))   # moved
        self.assertTrue(w.needs_repoint(r"C:\LDTF\runtime\pythonw.exe -m dtf_backup app", mine, exists, root))   # updated
        # another copy of LDTF (a download, a test run) must not take autostart over
        self.assertFalse(w.needs_repoint(r"E:\LDTF-2\runtime\pythonw.exe -m dtf_backup app", mine, exists, root))


if __name__ == "__main__":
    unittest.main()
