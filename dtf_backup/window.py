"""LDTF's own window on Windows: `LDTF.exe --window <url> --pid <app>` (tools/launcher/LDTF.cs), a WebView2 window on the
local site, one per installation, closed with the app. The app opens it instead of a browser tab when it runs from the
release (LDTF.exe and webview2/ next to it) and «Открывать в отдельном окне» is on (app settings, `app_window`).
Elsewhere — Docker, a server, Linux, macOS, a git checkout — the browser, as always.
"""

from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def launcher(root: Path = APP_ROOT) -> Path | None:
    """LDTF.exe of a release that can show the window, else None."""
    if os.name != "nt":
        return None
    exe = root / "LDTF.exe"
    return exe if exe.is_file() and (root / "webview2" / "Microsoft.Web.WebView2.Core.dll").is_file() else None


def key(root: Path = APP_ROOT) -> str:
    """FNV-1a of the installation folder, as LDTF.cs computes it (lower case, UTF-8, ending with a backslash)."""
    h = 2166136261
    for b in (str(root).rstrip("\\/") + "\\").lower().encode("utf-8"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return f"{h:08x}"


def handle_file(root: Path = APP_ROOT) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "LDTF" / "windows" / f"{key(root)}.txt"


def window_pid(root: Path = APP_ROOT) -> int:
    """The process of this installation's open window, 0 when there is none."""
    try:
        pid = int(handle_file(root).read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0
    from .sync import _pid_alive
    return pid if _pid_alive(pid) else 0


def open_window(url: str, app_pid: int | None = None, root: Path = APP_ROOT) -> bool:
    """Show `url` in LDTF's window (an open one takes the address and comes to the front). False: no window here."""
    exe = launcher(root)
    if exe is None:
        return False
    args = [str(exe), "--window", url] + (["--pid", str(app_pid)] if app_pid else [])
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP   # type: ignore[attr-defined]
    try:
        subprocess.Popen(args, cwd=str(root), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def show(url: str, app_pid: int | None = None, use_window: bool = True) -> None:
    """The app's pages: in its window when it can have one, else in the browser."""
    if not (use_window and open_window(url, app_pid)):
        webbrowser.open(url)
