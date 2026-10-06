"""Environment settings: the only place LDTF reads environment variables (Docker, servers, tests).

  LDTF_ROOT           folder of the archives (default: ./archive next to the app)
  LDTF_HOST           address to listen on (default 127.0.0.1; 0.0.0.0 in Docker)
  LDTF_PORT           port (default 8765; set explicitly, it is used as is)
  LDTF_PASSWORD       password of the web interface when it listens beyond this computer
  LDTF_PASSWORD_FILE  the same, read from a file (Docker secrets)
  LDTF_AUTH=off       no password beyond this computer: a reverse proxy in front of LDTF checks who comes in
  LDTF_DOCKER=1       set by the Docker image: no tray, autostart or self-update
  LDTF_MCP_TOKEN      the token of the MCP endpoint /mcp (default: made once, archive/.state/mcp.token)
  LDTF_UPDATE_URL     where the latest release is described (tests use a local fake of the GitHub API)
"""

from __future__ import annotations

import os
from pathlib import Path

RELEASES_API = "https://api.github.com/repos/Gwynerva/ldtf/releases/latest"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def flag(name: str) -> bool:
    return (env(name) or "").lower() in ("1", "true", "yes", "on")


def root() -> Path | None:
    v = env("LDTF_ROOT")
    return Path(v) if v else None


def host() -> str | None:
    return env("LDTF_HOST")


def port() -> int | None:
    v = env("LDTF_PORT")
    try:
        return int(v) if v else None
    except ValueError:
        raise SystemExit(f"LDTF_PORT должен быть числом, а не «{v}»") from None


def password() -> str | None:
    v = os.environ.get("LDTF_PASSWORD")   # not stripped: spaces may be part of a password
    if v:
        return v
    f = env("LDTF_PASSWORD_FILE")
    if f:
        try:
            return Path(f).read_text(encoding="utf-8").rstrip("\r\n") or None
        except OSError as e:
            raise SystemExit(f"Не удалось прочитать LDTF_PASSWORD_FILE ({f}): {e}") from None
    return None


def auth_off() -> bool:
    return (env("LDTF_AUTH") or "").lower() in ("off", "none", "0", "false", "no")


def docker() -> bool:
    return flag("LDTF_DOCKER")


def mcp_token() -> str | None:
    return env("LDTF_MCP_TOKEN")


def update_url() -> str:
    return env("LDTF_UPDATE_URL") or RELEASES_API


def is_loopback(host_: str) -> bool:
    return host_.strip("[]").lower() in LOOPBACK or host_.startswith("127.")
