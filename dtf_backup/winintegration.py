"""Windows integration: start LDTF with Windows (HKCU Run key) and shortcuts with the LDTF icon.

Everything here is a no-op or reports "unavailable" on other systems. The app runs windowless via pythonw.exe
from the embedded runtime (runtime/python314._pth adds the app folder to sys.path, so no working dir is needed).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
ICON = APP_ROOT / "dtf_backup" / "assets" / "brand" / "ldtf.ico"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "LDTF"


def available() -> bool:
    return os.name == "nt"


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


def set_autostart(on: bool, key: str = RUN_KEY) -> bool:
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
    return autostart_enabled(key)


def refresh_autostart(key: str = RUN_KEY) -> None:
    """The app folder was moved: point the existing autostart entry at the new place."""
    cur = autostart_value(key)
    if cur is not None and cur != command(background=True):
        set_autostart(True, key)


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
    env = dict(os.environ, LDTF_FOLDER=folder, LDTF_TARGET=str(pythonw()),
               LDTF_ARGS=subprocess.list2cmdline(launch_args()), LDTF_WORKDIR=str(APP_ROOT), LDTF_ICON=str(ICON))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        SHORTCUT_PS], env=env, capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out:
        raise OSError((r.stderr or "PowerShell не создал ярлык").strip()[:300])
    return Path(out[-1])
