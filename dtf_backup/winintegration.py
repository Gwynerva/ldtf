"""Windows integration: start LDTF with Windows (HKCU Run key) and shortcuts with the LDTF icon.

Everything here is a no-op or reports "unavailable" on other systems. A release starts with LDTF.exe (a launcher of
runtime/pythonw.exe, tools/launcher/LDTF.cs); a folder without it (a git clone) starts pythonw.exe itself — the
embedded runtime's python314._pth adds the app folder to sys.path, so no working dir is needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import log

APP_ROOT = Path(__file__).resolve().parent.parent
ICON = APP_ROOT / "dtf_backup" / "assets" / "brand" / "ldtf.ico"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Windows' own switch for the entries of RUN_KEY (Settings > Apps > Startup, Task Manager > Startup apps)
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE = "LDTF"
# autostart_state(): what the page and the log say
STATE_TITLES = {"on": "включён", "off": "выключен", "disabled": "выключен в автозагрузке Windows",
                "elsewhere": "настроен на другую копию LDTF"}
OWN_ENTRY = ("on", "disabled")   # states where this LDTF's entry is there: its switch shows "on"


def available() -> bool:
    return os.name == "nt"


def launcher(root: Path = APP_ROOT) -> Path | None:
    """LDTF.exe of a release (None in a git clone or on other systems)."""
    exe = root / "LDTF.exe"
    return exe if exe.exists() else None


def pythonw() -> Path:
    rt = APP_ROOT / "runtime" / "pythonw.exe"
    if rt.exists():
        return rt
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def launch_args(background: bool = False) -> list[str]:
    """Arguments after pythonw.exe that start the tray app from anywhere."""
    extra = ["--background"] if background else []
    if (APP_ROOT / "runtime" / "pythonw.exe").exists():
        return ["-X", "utf8", "-m", "dtf_backup", "app", *extra]
    # a system Python does not know the app folder: put it on sys.path first
    code = (f"import runpy,sys;sys.path.insert(0,r'{APP_ROOT}');sys.argv=['dtf_backup','app'{''.join(', ' + repr(e) for e in extra)}];"
            f"runpy.run_module('dtf_backup',run_name='__main__')")
    return ["-X", "utf8", "-c", code]


def command(background: bool = True) -> str:
    exe = launcher()
    if exe:
        return subprocess.list2cmdline([str(exe), *(["--background"] if background else [])])
    return subprocess.list2cmdline([str(pythonw()), *launch_args(background)])


# ---------------------------------------------------------------------- autostart
def autostart_value(key: str = RUN_KEY) -> str | None:
    if not available():
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return str(winreg.QueryValueEx(k, VALUE)[0])
    except OSError:
        return None


def autostart_enabled(key: str = RUN_KEY) -> bool:
    return autostart_value(key) is not None


def approved_value(key: str = APPROVED_KEY) -> bytes | None:
    """LDTF's mark in Windows' list of startup apps (None: never touched there)."""
    if not available():
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            v = winreg.QueryValueEx(k, VALUE)[0]
    except OSError:
        return None
    return bytes(v) if isinstance(v, (bytes, bytearray)) else None


def approved_disabled(mark: bytes | None) -> bool:
    """The mark is 12 bytes: a flag and the time of the change. An odd flag (01, 03, 07) = turned off in Windows: the
    Run entry stays, but Windows doesn't start it."""
    return bool(mark) and bool(mark[0] & 1)


def command_exe(cmd: str) -> str:
    """The program of a command line: `"C:/a b/x.exe" -arg` or `C:/x.exe -arg` (Windows paths)."""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        return cmd[1:].split('"', 1)[0]
    return cmd.split(" ", 1)[0]


def state_of(value: str | None, mark: bytes | None, wanted: str) -> str:
    """off | on | disabled (Windows skips it: turned off in its startup apps) | elsewhere (the entry starts another
    copy of LDTF)."""
    if value is None:
        return "off"
    if value != wanted:
        return "elsewhere"
    return "disabled" if approved_disabled(mark) else "on"


def autostart_state(key: str = RUN_KEY) -> str:
    if not available():
        return "off"
    return state_of(autostart_value(key), approved_value(), command(background=True))


def set_autostart(on: bool, key: str = RUN_KEY, source: str = "") -> bool:
    """Write (or remove) LDTF's entry; returns whether it is there now. Every change goes to the app log."""
    if not available():
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command(background=True))
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
    now = autostart_enabled(key)
    log.info(f"[автозапуск] {'включён' if now else 'выключен'}{f' ({source})' if source else ''}"
             + (f": {command(background=True)}" if now else ""))
    return now


def needs_repoint(value: str | None, wanted: str, exists: Any = os.path.exists, root: Path = APP_ROOT) -> bool:
    """The entry follows the app when the program it starts is gone (the app folder was moved or renamed) or lives in
    this folder (an update changed the arguments). Another copy of LDTF that runs for a while (a download, a test)
    must not take autostart over."""
    if value is None or value == wanted:
        return False
    exe = os.path.normcase(os.path.abspath(command_exe(value)))
    return not exists(exe) or exe.startswith(os.path.normcase(str(root)) + os.sep)


def refresh_autostart(key: str = RUN_KEY) -> None:
    """The app folder was moved: point the existing autostart entry at the new place."""
    if needs_repoint(autostart_value(key), command(background=True)):
        set_autostart(True, key, source="папка LDTF перенесена")


# ---------------------------------------------------------------------- shortcuts
SHORTCUT_PS = r"""
$sh = New-Object -ComObject WScript.Shell
$dir = [Environment]::GetFolderPath($env:LDTF_FOLDER)
$lnk = $sh.CreateShortcut((Join-Path $dir 'LDTF.lnk'))
$lnk.TargetPath = $env:LDTF_TARGET
$lnk.Arguments = $env:LDTF_ARGS
$lnk.WorkingDirectory = $env:LDTF_WORKDIR
$lnk.IconLocation = $env:LDTF_ICON + ',0'
$lnk.Description = 'LDTF — локальный DTF'
$lnk.Save()
Write-Output (Join-Path $dir 'LDTF.lnk')
"""


def create_shortcut(where: str) -> Path:
    """where: "desktop" | "startmenu". Returns the .lnk path."""
    if not available():
        raise OSError("ярлыки поддерживаются только в Windows")
    folder = {"desktop": "Desktop", "startmenu": "Programs"}[where]
    exe = launcher()
    target, args, ico = (exe, "", exe) if exe else (pythonw(), subprocess.list2cmdline(launch_args()), ICON)
    env = dict(os.environ, LDTF_FOLDER=folder, LDTF_TARGET=str(target), LDTF_ARGS=args, LDTF_WORKDIR=str(APP_ROOT),
               LDTF_ICON=str(ico))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        SHORTCUT_PS], env=env, capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out:
        raise OSError((r.stderr or "PowerShell не создал ярлык").strip()[:300])
    return Path(out[-1])
