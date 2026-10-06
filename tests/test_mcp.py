"""MCP of LDTF (dtf_backup/mcp): the dispatcher in both protocol eras, every tool on the fixture archive, the HTTP
endpoint's rules (token, Origin, 2026 headers, status codes) and the stdio server (also while the archive is rebuilt)."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dtf_backup.mcp.protocol import MODERN, Dispatcher  # noqa: E402
from dtf_backup.mcp.tools import TOOLS, Tools  # noqa: E402
from dtf_backup.render import render  # noqa: E402
from test_app import OWN_POST, make_archive  # noqa: E402

META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.library = Path(cls.tmp.name) / "archive"
        cls.arch = make_archive(cls.library)
        render(cls.arch)
        cls.arch.close()
        from dtf_backup.web.server import App, Handler
        cls.app = App(cls.library, 0)
        Handler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.disp = Dispatcher(Tools(cls.library))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import gc
        gc.collect()
        cls.tmp.cleanup()

    def rpc(self, method: str, params: dict | None = None, rid: int = 1) -> dict:
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return self.disp.handle(msg)

    def call(self, name: str, **args) -> tuple[str, bool]:
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r["isError"]

    # ------------------------------------------------------------------ dispatcher
    def test_legacy_handshake(self) -> None:
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        r = self.rpc("initialize", {"protocolVersion": "2024-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-11-25")       # the newest legacy one
        self.assertIsNone(self.disp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in TOOLS])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in TOOLS))

    def test_modern(self) -> None:
        r = self.rpc("server/discover", {"_meta": META})["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertIn(MODERN, r["supportedVersions"])
        self.assertIn("io.modelcontextprotocol/serverInfo", r["_meta"])
        r = self.rpc("tools/list", {"_meta": META})["result"]
        self.assertEqual((r["resultType"], r["cacheScope"]), ("complete", "public"))
        self.assertGreater(r["ttlMs"], 0)
        e = self.rpc("tools/list", {"_meta": dict(META, **{"io.modelcontextprotocol/protocolVersion": "1999-01-01"})})["error"]
        self.assertEqual(e["code"], -32022)
        self.assertIn(MODERN, e["data"]["supported"])
        self.assertEqual(self.rpc("nope/method", {"_meta": META})["error"]["code"], -32601)
        r = self.rpc("tools/call", {"name": "list_archives", "arguments": {}, "_meta": META})["result"]
        self.assertEqual(r["resultType"], "complete")

    def test_tools(self) -> None:
        text, err = self.call("list_archives")
        self.assertFalse(err)
        self.assertIn("@tester", text)
        text, err = self.call("search", query="катана")
        self.assertFalse(err, text)
        self.assertIn(f"Пост {OWN_POST}", text)
        self.assertIn("Комментарий 23", text)
        text, err = self.call("search", query="катана", where="posts", year=2026, sort="newest", limit=1)
        self.assertFalse(err)
        self.assertNotIn("Комментарий", text)
        text, err = self.call("get_post", post_id=OWN_POST, comments=True)
        self.assertFalse(err, text)
        self.assertIn("# Мой пост про катану", text)
        self.assertIn("донаты 500 ₽", text)
        self.assertIn("Первый!", text)
        self.assertIn("futureBlock", text)                       # unknown blocks come with their data
        text, err = self.call("get_comment", comment_id=24)
        self.assertFalse(err, text)
        self.assertIn("Ветка выше", text)
        self.assertIn("Возражение", text)
        text, err = self.call("list_posts", sort="donations")
        self.assertFalse(err)
        self.assertIn("донаты 500 ₽", text)
        text, err = self.call("list_comments", sort="old", limit=1)
        self.assertFalse(err)
        self.assertIn("Ещё: offset=1", text)
        text, err = self.call("get_history", kind="post", id=OWN_POST)
        self.assertFalse(err)
        self.assertIn("нет истории", text)
        text, err = self.call("archive_stats")
        self.assertFalse(err)
        self.assertIn("## Донаты", text)
        # mistakes come back as tool errors the agent can read
        for name, args in (("get_post", {"post_id": 1}), ("search", {"query": "x", "where": "everywhere"}),
                           ("list_posts", {"date_from": "вчера"}), ("get_history", {"kind": "user", "id": 1}),
                           ("list_archives", {"extra": 1}), ("search", {"query": "x", "archive": "nobody"})):
            text, err = self.call(name, **args)
            self.assertTrue(err, (name, text))
        self.assertEqual(self.rpc("tools/call", {"name": "drop_all", "arguments": {}})["error"]["code"], -32602)

    # ------------------------------------------------------------------ HTTP
    def post(self, body, headers: dict | None = None, token: bool = True, method: str = "POST") -> tuple[int, dict | str]:
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {self.app.mcp_token()}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else "")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode("utf-8", "replace")

    def test_http(self) -> None:
        legacy = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_archives", "arguments": {}}}
        self.assertEqual(self.post(legacy, token=False)[0], 401)
        code, r = self.post(legacy)
        self.assertEqual(code, 200)
        self.assertIn("@tester", r["result"]["content"][0]["text"])
        modern = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "search", "arguments": {"query": "катана"}, "_meta": META}}
        hdr = {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": "search"}
        code, r = self.post(modern, hdr)
        self.assertEqual((code, r["result"]["resultType"]), (200, "complete"))
        code, r = self.post(modern, dict(hdr, **{"Mcp-Name": "=?base64?c2VhcmNo?="}))     # "search", encoded
        self.assertEqual(code, 200)
        code, r = self.post(modern, dict(hdr, **{"Mcp-Name": "get_post"}))
        self.assertEqual((code, r["error"]["code"]), (400, -32020))
        code, r = self.post(modern, {"MCP-Protocol-Version": MODERN})
        self.assertEqual((code, r["error"]["code"]), (400, -32020))
        code, r = self.post(legacy, {"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual((code, r["error"]["code"]), (400, -32022))
        bogus = {"jsonrpc": "2.0", "id": 3, "method": "bogus/x", "params": {"_meta": META}}
        code, r = self.post(bogus, {"MCP-Protocol-Version": MODERN, "Mcp-Method": "bogus/x"})
        self.assertEqual((code, r["error"]["code"]), (404, -32601))
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertEqual(self.post(legacy, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(None, method="GET")[0], 405)
        self.assertEqual(self.post(None, method="DELETE")[0], 405)
        self.assertEqual(self.post([legacy])[0], 400)

    def test_agents_page(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/app/agents", timeout=20) as r:
            body = r.read().decode("utf-8")
        self.assertIn("claude mcp add", body)
        self.assertIn(f"http://127.0.0.1:{self.port}/mcp", body)
        self.assertIn(self.app.mcp_token(), body)
        self.assertIn("-m dtf_backup mcp", body)
        self.assertNotIn("pythonw", body)

    # ------------------------------------------------------------------ stdio
    def test_stdio_and_rebuild(self) -> None:
        """A client keeps the stdio server running; the archive can still be rebuilt (no file held open)."""
        p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "dtf_backup", "mcp", "--root", str(self.library)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT))
        try:
            def rpc(msg: dict) -> dict:
                p.stdin.write((json.dumps(msg) + "\n").encode())
                p.stdin.flush()
                return json.loads(p.stdout.readline())
            r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
            self.assertEqual(r["result"]["serverInfo"]["name"], "ldtf")
            r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "get_post", "arguments": {"post_id": OWN_POST}}})
            self.assertIn("катану", r["result"]["content"][0]["text"])
            from dtf_backup.state import Archive
            arch = Archive(self.library / "tester", self.library)
            render(arch)            # replaces view.sqlite while the server is up
            arch.close()
            r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "list_posts", "arguments": {}}})
            self.assertFalse(r["result"]["isError"])
            p.stdin.write(b"{not json\n")
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["error"]["code"], -32700)
        finally:
            p.stdin.close()
            p.wait(20)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
