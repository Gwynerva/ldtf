"""LDTF reachable over the network (serve --host 0.0.0.0, Docker): the password, sessions, what is open without them,
the same-origin rule for actions, the refusal to listen on the network without a password."""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup.render import render  # noqa: E402
from test_app import make_archive  # noqa: E402

PASSWORD = "сложный пароль 42"


class RemoteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.library = Path(cls.tmp.name) / "archive"
        arch = make_archive(cls.library)
        render(arch)
        arch.close()
        from dtf_backup.web.server import App, Handler
        cls.app = App(cls.library, 0, host="0.0.0.0", password=PASSWORD)

        class RemoteHandler(Handler):   # its own App: Handler.app belongs to the other tests
            app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import gc
        gc.collect()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.app.gate.fails, self.app.gate.until = 0, 0.0

    def req(self, method: str, path: str, body: str | bytes | None = None, headers: dict | None = None,
            host: str = "nas.local:8765") -> tuple[int, dict, str]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host}
        h.update(headers or {})
        if isinstance(body, str):
            body = body.encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def login(self) -> str:
        code, h, _ = self.req("POST", "/login", "password=" + PASSWORD.replace(" ", "+") + "&next=%2Farchives")
        self.assertEqual((code, h.get("location")), (303, "/archives"))
        self.assertIn("HttpOnly", h["set-cookie"])
        return h["set-cookie"].split(";")[0]

    def test_without_session(self) -> None:
        self.assertEqual(self.req("GET", "/healthz")[0], 200)
        code, h, _ = self.req("GET", "/u/tester/")
        self.assertEqual((code, h["location"]), (303, "/login?next=%2Fu%2Ftester%2F"))
        code, _, body = self.req("GET", "/login?next=/u/tester/")
        self.assertEqual(code, 200)
        self.assertIn('type="password"', body)
        code, _, body = self.req("GET", "/api/ping")
        self.assertEqual(code, 200)
        self.assertNotIn("pid", json.loads(body))
        for path in ("/media/ab/x.jpg", "/api/jobs", "/blocks"):
            code, h, _ = self.req("GET", path, headers={"Sec-Fetch-Mode": "cors"})
            self.assertEqual(code, 401, path)
        self.assertEqual(self.req("GET", "/assets/style.css")[0], 200)
        # actions need a session too (the CSRF token alone is not enough)
        code, _, _ = self.req("POST", "/app", f"_csrf={self.app.csrf}&autosync=0")
        self.assertEqual(code, 401)
        self.assertTrue(self.app.settings()["autosync"])

    def test_tokens_of_their_own(self) -> None:
        code, _, _ = self.req("GET", "/api/jobs", headers={"X-LDTF-Token": self.app.token})
        self.assertEqual(code, 200)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        code, _, _ = self.req("POST", "/mcp", body, {"Authorization": f"Bearer {self.app.mcp_token()}",
                                                     "Content-Type": "application/json"})
        self.assertEqual(code, 200)

    def test_login_session_logout(self) -> None:
        cookie = self.login()
        code, _, body = self.req("GET", "/archives", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertIn("Тестер", body)
        code, _, body = self.req("GET", "/app", headers={"Cookie": cookie})
        self.assertIn('action="/logout"', body)
        # a forged or expired cookie is no session
        exp, sig = cookie.split("=", 1)[1].split(".")
        for bad in (f"{exp}.{'0' * len(sig)}", f"{int(time.time()) - 10}.{self.app.gate._sig(int(time.time()) - 10)}"):
            self.assertEqual(self.req("GET", "/archives", headers={"Cookie": "ldtf_session=" + bad})[0], 303)
        # actions: same origin only
        code, _, _ = self.req("POST", "/app", f"_csrf={self.app.csrf}&notify=1",
                              {"Cookie": cookie, "Origin": "http://evil.example", "Accept": "application/json"})
        self.assertEqual(code, 403)
        code, _, _ = self.req("POST", "/app", f"_csrf={self.app.csrf}&notify=1",
                              {"Cookie": cookie, "Origin": "http://nas.local:8765", "Accept": "application/json"})
        self.assertEqual(code, 200)
        code, h, _ = self.req("POST", "/logout", f"_csrf={self.app.csrf}", {"Cookie": cookie})
        self.assertEqual((code, h["location"]), (303, "/login"))
        self.assertIn("Max-Age=0", h["set-cookie"])

    def test_wrong_passwords_slow_down(self) -> None:
        for i in range(5):
            self.assertEqual(self.req("POST", "/login", "password=nope")[0], 401)
        code, _, body = self.req("POST", "/login", "password=" + PASSWORD.replace(" ", "+"))
        self.assertEqual(code, 429)                   # even the right one waits
        self.assertIn("подождите", body)
        self.app.gate.until = 0
        self.login()

    def test_next_stays_on_site(self) -> None:
        code, h, _ = self.req("POST", "/login", "password=" + PASSWORD.replace(" ", "+") + "&next=%2F%2Fevil.example")
        self.assertEqual(h.get("location"), "/")

    def test_no_password_no_network(self) -> None:
        from dtf_backup.web.server import serve
        env = {k: v for k, v in os.environ.items() if not k.startswith("LDTF_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(serve(self.library / "empty", 0, host="0.0.0.0"), 1)


if __name__ == "__main__":
    unittest.main()
