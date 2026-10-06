"""The notes of one version from CHANGELOG.md (its "## X.Y.Z" section), for the GitHub release:

    python tools/release_notes.py 1.4.0 > notes.md
"""

from __future__ import annotations

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def notes(version: str) -> str:
    out: list[str] = []
    inside = False
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].split(" ")[0] == version
            continue
        if inside:
            out.append(line)
    text = "\n".join(out).strip()
    if not text:
        raise SystemExit(f"в CHANGELOG.md нет раздела ## {version}")
    return text + "\n"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # type: ignore[attr-defined]
    sys.stdout.write(notes(sys.argv[1]))
