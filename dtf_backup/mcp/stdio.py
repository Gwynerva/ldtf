"""`python -m dtf_backup mcp`: MCP over stdio — one JSON-RPC message per line on stdin, answers on stdout.

stdout carries nothing but protocol messages (bytes, "\\n" line ends, UTF-8); logs go to stderr. Use python.exe, not
pythonw.exe (the latter has no stdio on Windows). The archives are read from their view.sqlite files, so this works
whether LDTF itself is running or not.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .protocol import PARSE_ERROR, Dispatcher
from .tools import Tools


def run(library: Path) -> int:
    log = logging.getLogger("dtf_backup")
    log.handlers.clear()
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(logging.WARNING)
    log.addHandler(h)
    disp = Dispatcher(Tools(library))
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            resp: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": f"не JSON: {e}"}}
        else:
            resp = disp.handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()
    return 0
