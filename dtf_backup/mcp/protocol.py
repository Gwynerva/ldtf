"""JSON-RPC of MCP for both eras (one dispatcher; the transports add their rules):

- modern, 2026-07-28: no handshake; every request carries `_meta` with io.modelcontextprotocol/protocolVersion (and
  client info and capabilities); `server/discover` tells what the server speaks; results carry `resultType`, lists
  `ttlMs` and `cacheScope`;
- legacy, 2025-11-25 / 2025-06-18 / 2025-03-26: `initialize` picks the version, then `notifications/initialized`,
  `ping`, `tools/list`, `tools/call`.
Only tools are offered (read-only), every answer is a plain JSON object: no SSE streams, no sessions, no
server-initiated requests.
"""

from __future__ import annotations

from typing import Any

from .tools import INSTRUCTIONS, SERVER_INFO, TOOLS, Tools, call

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"
# JSON-RPC and MCP error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022
TTL_MS = 3_600_000


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def as_error(self) -> dict:
        e: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            e["data"] = self.data
        return e


def request_version(msg: dict) -> str | None:
    """The protocol version a modern request declares in its _meta (None: a legacy request)."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    v = meta.get(META_VERSION)
    return str(v) if v is not None else None


class Dispatcher:
    """One per stdio process, one per app (HTTP): `legacy` remembers the version an initialize picked (stdio)."""

    def __init__(self, tools: Tools):
        self.tools = tools
        self.legacy: str | None = None

    def handle(self, msg: Any) -> dict | None:
        """A JSON-RPC message -> the response (None for a notification)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": INVALID_REQUEST, "message": "ожидается один JSON-RPC 2.0 запрос"}}
        if "id" not in msg:   # notifications: initialized, cancelled, ... nothing to answer
            return None
        rid = msg["id"]
        try:
            result = self.result(msg)
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": e.as_error()}
        except Exception as e:  # noqa: BLE001 - a broken tool must not kill the server
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": INTERNAL, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def result(self, msg: dict) -> dict:
        method = msg["method"]
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        version = request_version(msg)
        modern = version is not None
        if modern and version not in SUPPORTED:
            raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version",
                           {"supported": list(SUPPORTED), "requested": version})
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            self.legacy = asked if asked in LEGACY else LEGACY[0]
            return {"protocolVersion": self.legacy, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        if method == "server/discover":
            return self._modern({"supportedVersions": list(SUPPORTED), "capabilities": {"tools": {}},
                                 "instructions": INSTRUCTIONS, "ttlMs": TTL_MS, "cacheScope": "public"})
        if method == "ping":
            return self._modern({}) if modern else {}
        if method == "tools/list":
            res = {"tools": TOOLS}
            return self._modern(dict(res, ttlMs=TTL_MS, cacheScope="public")) if modern else res
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                raise RpcError(INVALID_PARAMS, "нужны name и arguments")
            try:
                text, is_error = call(self.tools, name, args)
            except KeyError:
                raise RpcError(INVALID_PARAMS, f"нет инструмента {name}") from None
            res = {"content": [{"type": "text", "text": text}], "isError": is_error}
            return self._modern(res) if modern else res
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    @staticmethod
    def _modern(res: dict) -> dict:
        return {"resultType": "complete", **res, "_meta": {META_SERVER: SERVER_INFO}}
