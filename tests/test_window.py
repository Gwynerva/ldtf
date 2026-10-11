"""LDTF's own window (window.py + LDTF.exe --window) and how a failed start is reported (offline, no real window)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup import desktop, update, window  # noqa: E402


class WindowTest(unittest.TestCase):
    def test_key_is_the_one_ldtf_exe_computes(self) -> None:
        # FNV-1a of "c:\ldtf\" (lower case, UTF-8, trailing backslash) — LDTF.cs AppWindow.Key
        self.assertEqual(window.key(Path(r"C:\LDTF")), window.key(Path("c:\\ldtf\\")))
        h = 2166136261
        for b in "c:\\ldtf\\".encode():
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        self.assertEqual(window.key(Path(r"C:\LDTF")), f"{h:08x}")

    def test_only_a_release_on_windows_has_a_window(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(window.launcher(root))                     # a git checkout, Docker, a server
            (root / "LDTF.exe").write_bytes(b"MZ")
            self.assertIsNone(window.launcher(root))                     # no WebView2 SDK next to it
            (root / "webview2").mkdir()
            (root / "webview2" / "Microsoft.Web.WebView2.Core.dll").write_bytes(b"MZ")
            with mock.patch.object(window.os, "name", "posix"):
                self.assertIsNone(window.launcher(root))
            if sys.platform == "win32":
                self.assertEqual(window.launcher(root), root / "LDTF.exe")

    def test_show_falls_back_to_the_browser(self) -> None:
        with mock.patch.object(window, "open_window", return_value=False) as ow, \
                mock.patch.object(window.webbrowser, "open") as wb:
            window.show("http://127.0.0.1:8765/", 42)
            ow.assert_called_once()
            wb.assert_called_once_with("http://127.0.0.1:8765/")
        with mock.patch.object(window, "open_window") as ow, mock.patch.object(window.webbrowser, "open") as wb:
            window.show("http://127.0.0.1:8765/", 42, use_window=False)   # «Открывать в отдельном окне» is off
            ow.assert_not_called()
            wb.assert_called_once()

    def test_update_reopens_an_open_window(self) -> None:
        lib = update.APP_ROOT / "archive"
        with mock.patch("dtf_backup.window.window_pid", return_value=1234):
            self.assertEqual(update.restart_args(lib, None), ["--background", "--show"])
        with mock.patch("dtf_backup.window.window_pid", return_value=0):
            self.assertEqual(update.restart_args(lib, 8770), ["--background", "--port", "8770"])

    def test_failed_start_is_reported_once(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "archive"
            lib.mkdir()
            with mock.patch.object(desktop, "find_running", return_value=None), \
                    mock.patch.object(desktop, "check_writable", return_value="Нет прав на папку"):
                self.assertEqual(desktop.run_app(lib), desktop.REPORTED)
            with mock.patch.object(desktop, "find_running", return_value=None), \
                    mock.patch.object(desktop, "Runtime", side_effect=OSError(10048, "address in use")):
                self.assertEqual(desktop.run_app(lib, 8765), desktop.REPORTED)

    def test_second_start_shows_the_running_one(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            run = {"url": "http://127.0.0.1:8765/", "pid": 77}
            with mock.patch.object(desktop, "find_running", return_value=run), \
                    mock.patch.object(desktop.window, "show") as show:
                self.assertEqual(desktop.run_app(lib), 0)
                show.assert_called_once_with("http://127.0.0.1:8765/", 77, True)
                show.reset_mock()
                self.assertEqual(desktop.run_app(lib, background=True), 0)   # autostart: nothing to show
                show.assert_not_called()
                self.assertEqual(desktop.run_app(lib, background=True, show=True), 0)
                show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
