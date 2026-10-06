"""Updates (update.py, selfupdate.py): versions, how LDTF was installed, the download (hash, paths, contents) from a
local fake of GitHub, and the helper's swap, rollback and handling of a runtime file in use (Windows)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup import update  # noqa: E402
from dtf_backup.selfupdate import Helper  # noqa: E402


class VersionTest(unittest.TestCase):
    def test_versions(self) -> None:
        self.assertTrue(update.newer("1.4.1", "1.4.0"))
        self.assertTrue(update.newer("v2.0", "1.9.9"))
        self.assertFalse(update.newer("1.4.0", "1.4.0"))
        self.assertTrue(update.newer("1.5.0", "1.5.0-rc1"))     # a release after its pre-release
        self.assertFalse(update.newer("1.5.0-rc1", "1.5.0"))
        self.assertFalse(update.newer("garbage", "1.0.0"))

    def test_install_kind(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LDTF_DOCKER": ""}):
            root = Path(d)
            self.assertEqual(update.install_kind(root), "other")
            for n in ("release.json", "LDTF.exe", "runtime/pythonw.exe"):
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text("x")
            self.assertEqual(update.install_kind(root), "release" if os.name == "nt" else "other")
            (root / ".git").mkdir()
            self.assertEqual(update.install_kind(root), "git")      # a clone is never updated by the app
            with mock.patch.dict(os.environ, {"LDTF_DOCKER": "1"}):
                self.assertEqual(update.install_kind(root), "docker")

    def test_notes(self) -> None:
        h = update.notes_html("## 1.5.0\n\n- **Новое:** `x`\n  продолжение\n- <script>", lambda s: s.replace("<", "&lt;"))
        self.assertEqual(h, "<h3>1.5.0</h3><ul><li><b>Новое:</b> <code>x</code> продолжение</li><li>&lt;script></li></ul>")


def make_zip(version: str, extra: dict | None = None, platform: str = "windows-x64") -> bytes:
    files = {"LDTF/release.json": json.dumps({"version": version, "platform": platform, "runtime": "r1",
                                              "files": ["LDTF.exe", "dtf_backup", "runtime", "release.json"]}),
             "LDTF/LDTF.exe": "exe", "LDTF/runtime/pythonw.exe": "py", "LDTF/dtf_backup/__init__.py": f"__version__ = '{version}'"}
    files.update(extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


class DownloadTest(unittest.TestCase):
    """The app checks a fake GitHub (LDTF_UPDATE_URL) and downloads from it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.d = Path(self.tmp.name)
        (self.d / "srv").mkdir()
        srv_dir = str(self.d / "srv")

        class H(SimpleHTTPRequestHandler):
            def __init__(s, *a, **k):  # noqa: N805
                super().__init__(*a, directory=srv_dir, **k)

            def log_message(s, *a):  # noqa: N805
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.env = mock.patch.dict(os.environ, {"LDTF_UPDATE_URL": self.base + "/latest.json", "LDTF_DOCKER": ""})
        self.env.start()
        self.work = mock.patch.object(update, "WORK", self.d / "work")
        self.work.start()

    def tearDown(self) -> None:
        self.work.stop()
        self.env.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def publish(self, version: str, data: bytes, sums: str | None = None) -> None:
        name = f"LDTF-{version}-windows-x64.zip"
        (self.d / "srv" / name).write_bytes(data)
        (self.d / "srv" / "SHA256SUMS").write_text(sums or f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (self.d / "srv" / "latest.json").write_text(json.dumps({
            "tag_name": f"v{version}", "name": f"LDTF {version}", "body": "- новое", "html_url": "https://x/rel",
            "assets": [{"name": name, "size": len(data), "browser_download_url": f"{self.base}/{name}"},
                       {"name": "SHA256SUMS", "browser_download_url": f"{self.base}/SHA256SUMS"}]}))

    def updater(self) -> update.Updater:
        lib = self.d / "lib"
        (lib / ".state").mkdir(parents=True, exist_ok=True)
        return update.Updater(lib, lambda: {"update_check": True})

    def test_check_and_download(self) -> None:
        self.publish("9.9.0", make_zip("9.9.0"))
        u = self.updater()
        notes: list = []
        u.notify = lambda t, x: notes.append(t)
        s = u.check(force=True)
        self.assertTrue(s["available"])
        self.assertEqual(s["latest"]["version"], "9.9.0")
        self.assertEqual(notes, ["Доступна LDTF 9.9.0"])
        u.check(force=True)
        self.assertEqual(len(notes), 1)                              # one notice per version
        self.assertTrue(self.updater().available())                  # remembered (archive/.state/update.json)
        new = u.download()
        self.assertEqual(json.loads((new / "release.json").read_text())["version"], "9.9.0")

    def test_refuses_bad_downloads(self) -> None:
        cases = {
            "hash": (make_zip("9.9.1"), "0" * 64 + "  LDTF-9.9.1-windows-x64.zip\n"),
            "path": (make_zip("9.9.1", {"LDTF/../../evil.txt": "x"}), None),
            "version": (make_zip("9.9.0"), None),
            "platform": (make_zip("9.9.1", platform="linux"), None),
        }
        for why, (data, sums) in cases.items():
            self.publish("9.9.1", data, sums)
            u = self.updater()
            u.check(force=True)
            with self.assertRaises(RuntimeError, msg=why):
                u.download()
        self.assertFalse((self.d / "evil.txt").exists())

    def test_quiet_without_network(self) -> None:
        with mock.patch.dict(os.environ, {"LDTF_UPDATE_URL": "http://127.0.0.1:9/none.json"}):
            u = self.updater()
            s = u.check()
            self.assertEqual((s["state"], s["error"], s["available"]), ("idle", None, False))
            self.assertIn("Проверить не удалось", u.check(force=True)["error"])

    def test_only_github_without_test_server(self) -> None:
        with mock.patch.dict(os.environ, {"LDTF_UPDATE_URL": ""}):
            self.assertTrue(update._allowed("https://github.com/Gwynerva/ldtf/releases/download/v1/x.zip"))
            self.assertTrue(update._allowed("https://release-assets.githubusercontent.com/x"))
            self.assertFalse(update._allowed("http://github.com/x"))
            self.assertFalse(update._allowed("https://evil.example/x"))


class InstallFlowTest(unittest.TestCase):
    def test_waits_for_syncs_then_hands_over(self) -> None:
        """Downloaded and checked, the update waits while syncs run, then starts the helper and quits the app."""
        with tempfile.TemporaryDirectory() as d:
            u = update.Updater(Path(d), lambda: {})
            u.kind = "release"
            u.latest = {"version": "9.9.9"}
            states: list = []
            busy = iter([True, True, False])
            calls: list = []
            u.download = lambda: Path(d)                                   # type: ignore[method-assign]
            u.plan = lambda new, args, port: calls.append(("plan", args, port)) or Path(d) / "plan.json"   # type: ignore
            u.spawn = lambda plan: calls.append("spawn")                   # type: ignore[method-assign]
            real_set = u._set
            u._set = lambda **kw: (states.append(kw.get("state")), real_set(**kw))   # type: ignore[method-assign]
            with mock.patch.object(update.time, "sleep", lambda s: None):
                u.install(busy=lambda: next(busy), stop_jobs=None, args=["--background"], port=8765,
                          quit_app=lambda: calls.append("quit"))
                for _ in range(200):
                    if "quit" in calls:
                        break
                    threading.Event().wait(0.01)
            self.assertEqual([s for s in states if s], ["downloading", "waiting", "waiting", "installing"])
            self.assertEqual(calls, [("plan", ["--background"], 8765), "spawn", "quit"])

    def test_only_a_release_installs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            u = update.Updater(Path(d), lambda: {})
            u.kind = "git"
            with self.assertRaises(RuntimeError):
                u.install(busy=lambda: False, stop_jobs=None, args=[], port=1, quit_app=lambda: None)


class ResultTest(unittest.TestCase):
    """What the app says after an update: the new version knows it from the plan at once (the helper writes its
    result only after the new version answered); a rollback is told by the old version; leftovers go later."""

    def test_sequence(self) -> None:
        from dtf_backup import __version__
        with tempfile.TemporaryDirectory() as d, mock.patch.object(update, "WORK", Path(d)):
            w = Path(d)
            (w / "staging").mkdir()
            (w / "backup").mkdir()
            (w / "plan.json").write_text(json.dumps({"old_version": "0.9", "new_version": __version__}))
            self.assertEqual(update.take_result(), {"ok": True, "from": "0.9", "to": __version__})
            self.assertIsNone(update.take_result())                   # once
            (w / "result.json").write_text(json.dumps({"ok": True, "from": "0.9", "to": __version__}))
            self.assertIsNone(update.take_result())                   # the helper finished: clean, say nothing
            self.assertFalse((w / "staging").exists() or (w / "backup").exists() or (w / "plan.json").exists())
            (w / "backup").mkdir()
            (w / "result.json").write_text(json.dumps({"ok": False, "from": __version__, "to": "9.9", "error": "x"}))
            self.assertEqual(update.take_result()["error"], "x")      # the old version tells about the rollback
            self.assertTrue((w / "backup").exists())                  # kept: nothing was replaced for good


class HelperTest(unittest.TestCase):
    """The swap in a temporary app folder; LDTF.exe start and the ping are stubbed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.root, self.staging = d / "LDTF", d / "LDTF" / ".update" / "staging" / "LDTF"
        for base, ver in ((self.root, "1.0"), (self.staging, "2.0")):
            (base / "dtf_backup").mkdir(parents=True)
            (base / "dtf_backup" / "__init__.py").write_text(ver)
            (base / "runtime").mkdir()
            (base / "runtime" / "lib.dll").write_text(ver)
            (base / "LDTF.exe").write_text(ver)
            (base / "release.json").write_text(ver)
        (self.root / "old_only.txt").write_text("1.0")
        (self.root / "archive").mkdir()
        (self.root / "archive" / "keep.txt").write_text("user data")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.plan = {"pid": dead.pid, "app_root": str(self.root), "staging": str(self.staging),
                     "backup": str(self.root / ".update" / "backup" / "1.0"), "old_version": "1.0", "new_version": "2.0",
                     "old_files": ["LDTF.exe", "dtf_backup", "runtime", "release.json", "old_only.txt"],
                     "new_files": ["LDTF.exe", "dtf_backup", "runtime", "release.json"], "runtime_changed": False,
                     "args": ["--background"], "port": 1, "library": str(self.root / "archive"),
                     "result": str(self.root / ".update" / "result.json")}

    def tearDown(self) -> None:
        import gc
        gc.collect()
        self.tmp.cleanup()

    def run_helper(self, comes_up: bool) -> tuple[int, list]:
        started: list = []
        h = Helper(self.plan)
        h.start = lambda exe, args: started.append((Path(exe).read_text(), args))   # type: ignore[method-assign]
        h.wait_new = lambda: comes_up                                              # type: ignore[method-assign]
        code = h.run()
        h.logf.close()
        return code, started

    def test_swap(self) -> None:
        code, started = self.run_helper(True)
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "dtf_backup" / "__init__.py").read_text(), "2.0")
        self.assertEqual((self.root / "runtime" / "lib.dll").read_text(), "1.0")    # unchanged runtime stays
        self.assertFalse((self.root / "old_only.txt").exists())                     # gone from the release
        self.assertEqual((self.root / "archive" / "keep.txt").read_text(), "user data")
        self.assertEqual(started, [("2.0", ["--background"])])
        res = json.loads((self.root / ".update" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((res["ok"], res["to"]), (True, "2.0"))

    def test_rollback(self) -> None:
        self.plan["runtime_changed"] = True
        code, started = self.run_helper(False)
        self.assertEqual(code, 1)
        for p in ("dtf_backup/__init__.py", "runtime/lib.dll", "LDTF.exe", "old_only.txt"):
            self.assertEqual((self.root / p).read_text(), "1.0", p)
        self.assertEqual(started[-1], ("1.0", ["--background"]))                    # the old version again
        self.assertFalse(json.loads((self.root / ".update" / "result.json").read_text(encoding="utf-8"))["ok"])

    @unittest.skipUnless(os.name == "nt", "Windows file locks of running programs")
    def test_runtime_file_in_use(self) -> None:
        """A program of runtime/ still runs (an MCP client keeps python.exe): it is renamed aside, the new one goes in."""
        exe = self.root / "runtime" / "busy.exe"
        shutil.copy2(Path(os.environ["WINDIR"]) / "System32" / "ping.exe", exe)
        (self.staging / "runtime" / "busy.exe").write_bytes(b"new")
        self.plan["runtime_changed"] = True
        p = subprocess.Popen([str(exe), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            code, _ = self.run_helper(True)
            self.assertEqual(code, 0)
            self.assertEqual(exe.read_bytes(), b"new")
            self.assertTrue(list((self.root / "runtime").glob("busy.exe.old-1.0")))
        finally:
            p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
