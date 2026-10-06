"""Updates of LDTF: which version is the latest, and for a release folder on Windows, installing it from the app.

How LDTF was installed decides what an update is (install_kind):
  release  the zip of a release (release.json, runtime/, LDTF.exe): downloaded, checked and installed from the app
  git      a clone of the repository: `git pull`
  docker   the container: `docker compose pull && docker compose up -d`
  other    the release page
The check asks GitHub for the latest release once a day (setting `update_check`) or when asked, quietly giving up
without a network. Installing: the zip and SHA256SUMS of the release are downloaded (HTTPS, github.com only, size
capped), the hash checked, the zip unpacked into .update/staging (paths checked), its release.json compared; then
a helper process started from the NEW runtime (dtf_backup/selfupdate.py) waits for this app to exit, swaps the files,
starts the new version and rolls everything back if it does not come up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from . import __version__, config
from .util import log, now_ts, write_json

APP_ROOT = Path(__file__).resolve().parent.parent
WORK = APP_ROOT / ".update"
PLATFORM = "windows-x64"
CHECK_EVERY = 20 * 3600          # a daily check, a little early so the same hour each day works
MAX_ZIP = 300 * 1024 * 1024
ALLOWED_HOSTS = ("github.com", "api.github.com")   # and *.githubusercontent.com (release files)
RELEASES = "https://github.com/Gwynerva/ldtf/releases"


def install_kind(root: Path = APP_ROOT) -> str:
    if config.docker():
        return "docker"
    if (root / ".git").exists():
        return "git"
    if os.name == "nt" and all((root / n).exists() for n in ("release.json", "LDTF.exe", "runtime/pythonw.exe")):
        return "release"
    return "other"


def parse_version(v: str) -> tuple[int, int, int, int]:
    """1.4.0 -> (1, 4, 0, 1); a pre-release (1.5.0-rc1, 1.4.1-test) sorts before its release: (1, 5, 0, 0)."""
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?(.*)$", (v or "").strip())
    if not m:
        return (0, 0, 0, 0)
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), 0 if m.group(4) else 1


def newer(v: str, than: str = __version__) -> bool:
    return parse_version(v) > parse_version(than)


def _allowed(url: str) -> bool:
    if config.update_url() != config.RELEASES_API:   # a test release server (LDTF_UPDATE_URL): any address
        return True
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host in ALLOWED_HOSTS or host.endswith(".githubusercontent.com"))


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not _allowed(newurl):
            raise urllib.error.URLError(f"переадресация на {urllib.parse.urlsplit(newurl).hostname} отклонена")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float = 15, accept: str = "application/octet-stream") -> Any:
    if not _allowed(url):
        raise ValueError(f"адрес {url} не похож на GitHub")
    req = urllib.request.Request(url, headers={"User-Agent": f"LDTF/{__version__}", "Accept": accept})
    return urllib.request.build_opener(_Redirects()).open(req, timeout=timeout)


def release_info(data: dict) -> dict:
    """What LDTF needs of GitHub's description of a release."""
    tag = str(data.get("tag_name") or "")
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    z = next((a for n, a in assets.items() if n and n.endswith(f"-{PLATFORM}.zip")), None)
    sums = assets.get("SHA256SUMS")
    return {"version": tag.lstrip("v"), "tag": tag, "name": data.get("name") or tag, "notes": data.get("body") or "",
            "url": data.get("html_url") or f"{RELEASES}/tag/{tag}", "published": data.get("published_at"),
            "zip": z.get("browser_download_url") if z else None, "zip_name": z.get("name") if z else None,
            "zip_size": int(z.get("size") or 0) if z else 0, "sums": sums.get("browser_download_url") if sums else None}


class Updater:
    """One per app: the state the settings page shows (snapshot), the daily check, download and install."""

    def __init__(self, library: Path, settings: Any, notify: Any = None):
        self.library = library
        self.settings = settings            # () -> app settings (update_check)
        self.notify = notify                # (title, text) -> None: the tray
        self.kind = install_kind()
        self.lock = threading.Lock()
        self.cache = library / ".state" / "update.json"
        self.state: dict[str, Any] = {"state": "idle", "progress": None, "error": None}
        self.latest: dict | None = None
        self.checked_at: int | None = None
        try:
            c = json.loads(self.cache.read_text(encoding="utf-8"))
            self.latest, self.checked_at = c.get("latest"), c.get("checked_at")
            self.notified = c.get("notified")
        except (OSError, ValueError):
            self.notified = None
        self._stop = threading.Event()
        self._busy = threading.Lock()

    # ------------------------------------------------------------------ state
    def available(self) -> bool:
        return bool(self.latest and self.latest.get("version") and newer(self.latest["version"]))

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.state)
        s.update(kind=self.kind, current=__version__, latest=self.latest, checked_at=self.checked_at,
                 available=self.available(), can_install=self.kind == "release" and bool((self.latest or {}).get("zip")))
        return s

    def _set(self, **kw: Any) -> None:
        with self.lock:
            self.state.update(kw)

    def _save(self) -> None:
        try:
            write_json(self.cache, {"checked_at": self.checked_at, "latest": self.latest, "notified": self.notified})
        except OSError as e:
            log.warning(f"[обновление] не удалось сохранить {self.cache}: {e}")

    # ------------------------------------------------------------------ check
    def check(self, force: bool = False) -> dict:
        if not force and self.checked_at and now_ts() - self.checked_at < CHECK_EVERY:
            return self.snapshot()
        if not self._busy.acquire(blocking=False):
            return self.snapshot()
        try:
            self._set(state="checking", error=None)
            try:
                with _open(config.update_url(), timeout=10, accept="application/vnd.github+json") as r:
                    info = release_info(json.load(r))
            except Exception as e:  # noqa: BLE001 - no network, GitHub's limits: try again later, quietly
                log.info(f"[обновление] проверить не удалось: {e}")
                self._set(state="idle", error=f"Проверить не удалось: {e}" if force else None)
                return self.snapshot()
            self.latest, self.checked_at = info, now_ts()
            self._save()
            self._set(state="idle")
            if self.available():
                log.info(f"[обновление] доступна версия {info['version']} (установлена {__version__})")
                if self.notify and self.notified != info["version"]:
                    self.notified = info["version"]
                    self._save()
                    try:
                        self.notify(f"Доступна LDTF {info['version']}", "Обновить можно в настройках приложения.")
                    except Exception:  # noqa: BLE001
                        pass
            return self.snapshot()
        finally:
            self._busy.release()

    def start(self, first_delay: float = 120.0) -> None:
        """The daily check (when the setting allows it)."""
        def loop() -> None:
            if self._stop.wait(first_delay):
                return
            while True:
                if self.settings().get("update_check", True):
                    self.check()
                if self._stop.wait(3600):
                    return
        threading.Thread(target=loop, name="update-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ download + install (a release on Windows)
    def download(self) -> Path:
        """The release zip, checked against SHA256SUMS and unpacked into .update/staging; returns staging/LDTF."""
        info = self.latest or {}
        if not (info.get("zip") and info.get("sums")):
            raise RuntimeError("у релиза нет архива для Windows или файла SHA256SUMS")
        if info.get("zip_size") and info["zip_size"] > MAX_ZIP:
            raise RuntimeError("архив релиза подозрительно большой")
        WORK.mkdir(exist_ok=True)
        for d in ("staging", "backup"):
            shutil.rmtree(WORK / d, ignore_errors=True)
        with _open(info["sums"], timeout=30) as r:
            sums = r.read(100_000).decode("utf-8", "replace")
        want = next((line.split()[0].lower() for line in sums.splitlines()
                     if len(line.split()) == 2 and line.split()[1].lstrip("*") == info["zip_name"]), None)
        if not want or not re.fullmatch(r"[0-9a-f]{64}", want):
            raise RuntimeError(f"в SHA256SUMS нет строки для {info['zip_name']}")
        z = WORK / info["zip_name"]
        h = hashlib.sha256()
        done = 0
        with _open(info["zip"], timeout=60) as r, open(z, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.get("zip_size") or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_ZIP:
                    raise RuntimeError("архив релиза подозрительно большой")
                f.write(chunk)
                h.update(chunk)
                if total:
                    self._set(progress=round(100 * done / total))
        if h.hexdigest() != want:
            z.unlink(missing_ok=True)
            raise RuntimeError("контрольная сумма архива не совпала с SHA256SUMS — файл повреждён или подменён")
        staging = WORK / "staging"
        base = staging.resolve()
        with zipfile.ZipFile(z) as zf:
            for m in zf.infolist():
                target = (staging / m.filename).resolve()
                if not m.filename.startswith("LDTF/") or not str(target).startswith(str(base) + os.sep):
                    raise RuntimeError(f"в архиве посторонний путь: {m.filename}")
            zf.extractall(staging)
        z.unlink(missing_ok=True)
        new = staging / "LDTF"
        try:
            meta = json.loads((new / "release.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("в архиве нет release.json") from None
        if meta.get("version") != info["version"] or meta.get("platform") != PLATFORM:
            raise RuntimeError(f"в архиве версия {meta.get('version')} {meta.get('platform')}, ожидалась {info['version']}")
        for need in ("LDTF.exe", "runtime/pythonw.exe", "dtf_backup/__init__.py"):
            if not (new / need).exists():
                raise RuntimeError(f"в архиве нет {need}")
        return new

    def plan(self, new: Path, args: list[str], port: int) -> Path:
        """What the helper does (.update/plan.json)."""
        old_meta = json.loads((APP_ROOT / "release.json").read_text(encoding="utf-8"))
        new_meta = json.loads((new / "release.json").read_text(encoding="utf-8"))
        plan = {"pid": os.getpid(), "app_root": str(APP_ROOT), "staging": str(new),
                "backup": str(WORK / "backup" / __version__), "old_version": __version__,
                "new_version": new_meta["version"], "old_files": old_meta.get("files") or [],
                "new_files": new_meta.get("files") or [], "runtime_changed": old_meta.get("runtime") != new_meta.get("runtime"),
                "args": args, "port": port, "library": str(self.library), "result": str(WORK / "result.json")}
        p = WORK / "plan.json"
        write_json(p, plan)
        return p

    def spawn(self, plan: Path) -> None:
        """The helper runs from the new runtime (nothing of this folder stays in use) and outlives this app."""
        staging = Path(json.loads(plan.read_text(encoding="utf-8"))["staging"])
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen([str(staging / "runtime" / "pythonw.exe"), "-X", "utf8", "-m", "dtf_backup.selfupdate", str(plan)],
                         cwd=str(staging), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def install(self, busy: Any, stop_jobs: Any, args: list[str], port: int, quit_app: Any) -> None:
        """Download, wait for running syncs (`busy()`; the page may ask to stop them: stop_jobs), hand over to the
        helper and quit. Runs in a thread; the state tells the page how far it got."""
        if self.kind != "release":
            raise RuntimeError("обновление из приложения есть только у версии для Windows из архива релиза")
        if not self._busy.acquire(blocking=False):
            return

        def run() -> None:
            try:
                self._set(state="downloading", progress=0, error=None)
                new = self.download()
                while busy():
                    self._set(state="waiting", progress=None)
                    time.sleep(2)
                self._set(state="installing", progress=None)
                log.info(f"[обновление] устанавливаю {self.latest['version']}: LDTF перезапустится")
                self.spawn(self.plan(new, args, port))
                time.sleep(0.5)
                quit_app()
            except Exception as e:  # noqa: BLE001 - shown on the page, the app keeps working
                log.exception("[обновление] не удалось")
                self._set(state="error", error=str(e), progress=None)
            finally:
                self._busy.release()
        threading.Thread(target=run, name="update-install", daemon=True).start()


# ---------------------------------------------------------------------- after a start
def take_result() -> dict | None:
    """What to tell about the last update, once: the new version knows it from the plan (the helper is still waiting
    for it to answer and writes its result only then); a rollback's result is written before the old version starts
    again. A finished helper's leftovers (staging, plan, the backup of a good update) go on the next start."""
    def read(name: str) -> dict | None:
        try:
            return json.loads((WORK / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    res, plan = read("result.json"), read("plan.json")
    shown = None
    if res is not None:   # the helper has finished
        (WORK / "result.json").unlink(missing_ok=True)
        if res.get("ok"):
            shutil.rmtree(WORK / "backup", ignore_errors=True)
        else:
            shown = res
        for name in ("staging", "plan.json"):
            q = WORK / name
            shutil.rmtree(q, ignore_errors=True) if q.is_dir() else q.unlink(missing_ok=True)
    elif plan and plan.get("new_version") == __version__:   # this start is the update
        shown = {"ok": True, "from": plan.get("old_version"), "to": __version__}
        try:
            write_json(WORK / "plan.json", dict(plan, started=True))
        except OSError:
            pass
        if plan.get("started"):   # already shown at an earlier start
            shown = None
    for old in (APP_ROOT / "runtime").glob("*.old-*") if (APP_ROOT / "runtime").is_dir() else []:
        try:
            old.unlink()   # files of the previous runtime that were in use during the update
        except OSError:
            pass
    return shown


def restart_args(library: Path, port: int | None) -> list[str]:
    """How this app was started, for the new version: LDTF.exe --background [--root] [--port]."""
    out = ["--background"]
    if library.resolve() != (APP_ROOT / "archive").resolve():
        out += ["--root", str(library)]
    if port:
        out += ["--port", str(port)]
    return out


def notes_html(md: str, esc: Any) -> str:
    """The release notes (CHANGELOG sections are Markdown: headings, lists, **bold**, `code`) as simple HTML."""
    out, in_list = [], False
    for line in (md or "").replace("\r", "").split("\n"):
        s = line.strip()
        if s.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(s[2:], esc)}</li>")
            continue
        if line.startswith("  ") and in_list and out and s:
            out[-1] = out[-1][:-5] + " " + _inline(s, esc) + "</li>"
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if s.startswith("#"):
            out.append(f"<h3>{_inline(s.lstrip('#').strip(), esc)}</h3>")
        elif s:
            out.append(f"<p>{_inline(s, esc)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _inline(s: str, esc: Any) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)

