"""Build the Windows release of LDTF: dist/LDTF-<version>-windows-x64.zip + dist/SHA256SUMS.

    python tools/build_release.py                    # files of HEAD (git archive), Python downloaded from python.org
    python tools/build_release.py --runtime runtime  # an existing embedded Python instead of the download
    python tools/build_release.py --worktree         # the working tree (uncommitted changes too) - for local tests

The zip holds one folder, LDTF/: LDTF.exe (the launcher, tools/launcher/LDTF.cs built with csc.exe of .NET Framework 4,
Windows only), runtime/ (the embeddable Python with the app folder on its path), the app (git archive: .gitattributes
export-ignore keeps tests, tools and the Docker files out), .mcp.json (the MCP server for agents started in the folder)
and release.json — what the in-app updater manages:
{version, platform, python, runtime (fingerprint), files (top-level entries)}.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "3.14.8"   # the embeddable Python of the release; the hash was taken from python.org over HTTPS
PYTHON_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip"
PLATFORM = "windows-x64"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
PTH = """python{v}.zip
.
# LDTF: the app (dtf_backup/) is in the folder above
..

# Uncomment to run site.main() automatically
#import site
"""
# .mcp.json: Claude Code and other agents started in the LDTF folder find the MCP server by themselves; the command is
# relative to that folder and reads the library next to it (archive/) - as "Настройки приложения → ИИ-агенты" shows
MCP_JSON = {"mcpServers": {"ldtf": {"command": "runtime\\python.exe", "args": ["-X", "utf8", "-m", "dtf_backup", "mcp"]}}}


def version() -> str:
    ns: dict = {}
    exec((ROOT / "dtf_backup" / "__init__.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def app_files(dest: Path, worktree: bool) -> None:
    """The app as git sees it: HEAD (git archive, export-ignore applies) or the tracked files of the working tree."""
    if worktree:   # git archive's export-ignore, for the simple patterns .gitattributes uses (dir/, name, glob)
        names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
        rules = [line.split()[0] for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#") and "export-ignore" in line.split()[1:]]

        def ignored(n: str) -> bool:
            return any(n.startswith(r) if r.endswith("/") else fnmatch.fnmatch(n.split("/")[0], r) for r in rules)
        for n in names:
            if n and not ignored(n) and (ROOT / n).is_file():
                (dest / n).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / n, dest / n)
        return
    tar = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(dest, filter="data")


def runtime(dest: Path, src: Path | None, cache: Path) -> str:
    """runtime/: the embeddable Python (downloaded and checked, or copied) with LDTF's ._pth. Returns its version."""
    if src:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        out = subprocess.run([str(dest / "python.exe"), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, check=True).stdout.strip()
        pyver = out
    else:
        z = cache / f"python-{PYTHON}-embed-amd64.zip"
        if not z.exists():
            print(f"скачиваю {PYTHON_URL}")
            with urllib.request.urlopen(PYTHON_URL, timeout=120) as r:
                z.write_bytes(r.read())
        digest = hashlib.sha256(z.read_bytes()).hexdigest()
        if digest != PYTHON_SHA256:
            z.unlink()
            raise SystemExit(f"{z.name}: sha256 {digest}, ожидался {PYTHON_SHA256}")
        with zipfile.ZipFile(z) as f:
            f.extractall(dest)
        pyver = PYTHON
    pth = next(dest.glob("python3*._pth"))
    short = pth.stem.replace("python", "")
    pth.write_text(PTH.format(v=short), encoding="utf-8", newline="\r\n")
    return pyver


def fingerprint(folder: Path) -> str:
    """Changes when any file of the runtime changes (the updater replaces runtime/ only then)."""
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:16]


def launcher(dest: Path, ver: str, tmp: Path) -> None:
    if not CSC.exists():
        raise SystemExit(f"нет {CSC}: LDTF.exe собирается в Windows (csc.exe из .NET Framework 4)")
    info = tmp / "AssemblyInfo.cs"
    v4 = ".".join((ver.split("-")[0].split(".") + ["0", "0", "0"])[:4])
    info.write_text(
        "using System.Reflection;\n"
        '[assembly: AssemblyTitle("LDTF")]\n'
        '[assembly: AssemblyDescription("LDTF — локальный архив DTF")]\n'
        '[assembly: AssemblyProduct("LDTF")]\n'
        '[assembly: AssemblyCompany("Gwynerva")]\n'
        f'[assembly: AssemblyCopyright("© {_dt.date.today().year} Gwynerva, MIT")]\n'
        f'[assembly: AssemblyVersion("{v4}")]\n[assembly: AssemblyFileVersion("{v4}")]\n'
        f'[assembly: AssemblyInformationalVersion("{ver}")]\n', encoding="utf-8")
    cmd = [str(CSC), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", "/codepage:65001",
           f"/win32icon:{ROOT / 'dtf_backup' / 'assets' / 'brand' / 'ldtf.ico'}", f"/out:{dest / 'LDTF.exe'}",
           str(ROOT / "tools" / "launcher" / "LDTF.cs"), str(info)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("csc: " + (r.stdout + r.stderr).strip())


def build(args: argparse.Namespace) -> Path:
    ver = version()
    out = Path(args.out).resolve()
    stage = out / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    app = stage / "LDTF"
    app.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    app_files(app, args.worktree)
    pyver = runtime(app / "runtime", Path(args.runtime).resolve() if args.runtime else None, out)
    launcher(app, ver, stage)
    (app / ".mcp.json").write_text(json.dumps(MCP_JSON, indent=2) + "\n", encoding="utf-8")
    files = sorted(p.name for p in app.iterdir())
    meta = {"version": ver, "platform": PLATFORM, "python": pyver, "runtime": fingerprint(app / "runtime"),
            "built": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "files": files + ["release.json"]}
    (app / "release.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    name = out / f"LDTF-{ver}-{PLATFORM}.zip"
    name.unlink(missing_ok=True)
    with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, Path("LDTF") / p.relative_to(app))
    digest = hashlib.sha256(name.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {name.name}\n", encoding="utf-8", newline="\n")
    shutil.rmtree(stage, ignore_errors=True)
    print(f"{name} ({name.stat().st_size / 1e6:.1f} МБ), sha256 {digest}")
    return name


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runtime", help="папка встроенного Python вместо скачивания с python.org")
    ap.add_argument("--worktree", action="store_true", help="файлы рабочей копии (с незакоммиченными), а не HEAD")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="куда положить zip и SHA256SUMS (по умолчанию dist/)")
    build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
