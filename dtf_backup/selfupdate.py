"""The update helper: `runtime\\pythonw.exe -m dtf_backup.selfupdate <.update/plan.json>`, started by update.py from the
NEW version's folder (.update/staging/LDTF), so nothing of the app folder is in use by it.

1. waits until the old app has exited;
2. moves the app's managed top-level entries (release.json "files" of both versions) to .update/backup/<old version>
   — archive/ and anything else of the user's stays where it is;
3. copies the new version in; runtime/ only when it changed, file by file: a file still in use (an MCP client keeps
   python.exe and its DLLs loaded) is renamed to <name>.old-<version> first, which Windows allows;
4. starts LDTF.exe and waits up to a minute for the new version to answer /api/ping;
5. anything wrong: puts the backup back, starts the old version. The outcome goes to .update/result.json (the app
   shows it once) and the steps to .update/update.log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

DETACHED = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


class Helper:
    def __init__(self, plan: dict):
        self.p = plan
        self.root = Path(plan["app_root"])
        self.staging = Path(plan["staging"])
        self.backup = Path(plan["backup"])
        self.work = self.root / ".update"
        self.moved: list[str] = []
        self.copied: list[str] = []
        self.renamed: list[tuple[Path, Path]] = []
        self.logf = open(self.work / "update.log", "a", encoding="utf-8")

    def log(self, msg: str) -> None:
        self.logf.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        self.logf.flush()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def retry(fn, tries: int = 20, pause: float = 0.5) -> None:   # type: ignore[no-untyped-def]
        """Antivirus scanners and the indexer hold new files for a moment."""
        for i in range(tries):
            try:
                fn()
                return
            except PermissionError:
                if i == tries - 1:
                    raise
                time.sleep(pause)

    @staticmethod
    def alive(pid: int) -> bool:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32   # type: ignore[attr-defined]
            h = k.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def managed(self) -> list[str]:
        names = set(self.p["old_files"]) | set(self.p["new_files"])
        names.discard("archive")   # never: the user's archives (only if a release ever named it)
        return sorted(n for n in names if n and "/" not in n and "\\" not in n and n not in (".update", ".."))

    # ------------------------------------------------------------------ steps
    def wait_old(self) -> None:
        pid = int(self.p["pid"])
        deadline = time.time() + 120
        while self.alive(pid):
            if time.time() > deadline:
                raise RuntimeError(f"старая версия (PID {pid}) не завершилась за 2 минуты")
            time.sleep(0.3)
        self.log(f"старая версия завершилась (PID {pid})")

    def swap(self) -> None:
        self.backup.mkdir(parents=True, exist_ok=True)
        runtime_changed = bool(self.p["runtime_changed"])
        for name in self.managed():
            if name == "runtime":
                continue   # below
            src = self.root / name
            if src.exists():
                self.retry(lambda: os.replace(src, self.backup / name))
                self.moved.append(name)
        for name in self.managed():
            if name == "runtime" or name not in self.p["new_files"]:
                continue
            src = self.staging / name
            if not src.exists():
                continue
            dst = self.root / name
            if src.is_dir():
                self.retry(lambda: shutil.copytree(src, dst))
            else:
                self.retry(lambda: shutil.copy2(src, dst))
            self.copied.append(name)
        if runtime_changed:
            self.swap_runtime()
        self.log(f"файлы заменены: {', '.join(self.copied)}" + ("; runtime обновлён" if runtime_changed else ""))

    def swap_runtime(self) -> None:
        old, new = self.root / "runtime", self.staging / "runtime"
        shutil.copytree(old, self.backup / "runtime")   # reading works even while files are in use
        ver = self.p["old_version"]
        for f in sorted(old.rglob("*"), reverse=True):   # what the new runtime doesn't have
            rel = f.relative_to(old)
            if f.is_file() and not (new / rel).exists():
                self._replace_file(f, None, ver)
        for f in sorted(new.rglob("*")):
            rel = f.relative_to(new)
            dst = old / rel
            if f.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                self._replace_file(dst, f, ver)

    def _replace_file(self, dst: Path, src: Path | None, ver: str) -> None:
        try:
            if src is None:
                dst.unlink()
            else:
                tmp = dst.with_name(dst.name + ".new")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
        except PermissionError:   # loaded by a running process: move it aside, it goes on the next start
            aside = dst.with_name(f"{dst.name}.old-{ver}")
            aside.unlink(missing_ok=True)
            os.replace(dst, aside)
            self.renamed.append((aside, dst))
            if src is not None:
                shutil.copy2(src, dst)
            self.log(f"занят, отложен: {dst.name}")

    def start(self, exe: Path, args: list[str]) -> None:
        subprocess.Popen([str(exe), *args], cwd=str(self.root), creationflags=DETACHED if os.name == "nt" else 0,
                         close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_new(self) -> bool:
        run = Path(self.p["library"]) / ".state" / "app.run.json"
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(1)
            try:
                port = int(json.loads(run.read_text(encoding="utf-8"))["port"])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                    if json.load(r).get("version") == self.p["new_version"]:
                        return True
            except Exception:  # noqa: BLE001 - not up yet
                continue
        return False

    def rollback(self) -> None:
        self.log("откат: возвращаю прежнюю версию")
        for name in self.copied:
            p = self.root / name
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        for name in self.moved:
            self.retry(lambda: os.replace(self.backup / name, self.root / name))
        if self.p["runtime_changed"] and (self.backup / "runtime").exists():
            for f in sorted((self.backup / "runtime").rglob("*")):
                if f.is_file():
                    dst = self.root / "runtime" / f.relative_to(self.backup / "runtime")
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dst)
                    except PermissionError:
                        pass

    def result(self, ok: bool, error: str = "") -> None:
        Path(self.p["result"]).write_text(json.dumps(
            {"ok": ok, "from": self.p["old_version"], "to": self.p["new_version"], "error": error or None,
             "at": int(time.time())}, ensure_ascii=False), encoding="utf-8")

    def run(self) -> int:
        self.log(f"обновление {self.p['old_version']} → {self.p['new_version']}")
        try:
            self.wait_old()
            self.swap()
            self.start(self.root / "LDTF.exe", self.p["args"])
            if self.wait_new():
                self.result(True)
                self.log("новая версия запущена")
                return 0
            raise RuntimeError("новая версия не ответила за минуту")
        except Exception as e:  # noqa: BLE001
            self.log("ошибка: " + "".join(traceback.format_exception(e)))
            self.stop_new()
            try:
                self.rollback()
            except Exception as e2:  # noqa: BLE001
                self.log("откат не удался: " + repr(e2))
            self.result(False, str(e))
            exe = self.root / "LDTF.exe"
            if exe.exists():
                self.start(exe, self.p["args"])
            return 1

    def stop_new(self) -> None:
        """A new version that started but doesn't answer must not keep the files busy during the rollback."""
        try:
            info = json.loads((Path(self.p["library"]) / ".state" / "app.run.json").read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
            if pid and pid != int(self.p["pid"]) and self.alive(pid) and os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
                time.sleep(1)
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    plan = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    return Helper(plan).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
