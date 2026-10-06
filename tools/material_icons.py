"""Add Material Symbols Rounded icons to dtf_backup/web/icons.py (path data from the npm package via jsDelivr).

    python tools/material_icons.py volunteer_activism history [name-fill ...]

Names ending with "-fill" take the filled variant. The package version stays the one icons.py names, so every icon
of the app comes from one release.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ICONS = Path(__file__).resolve().parent.parent / "dtf_backup" / "web" / "icons.py"
PKG = "@material-symbols/svg-400@0.47.5"
URL = "https://cdn.jsdelivr.net/npm/" + PKG + "/rounded/{name}.svg"
ENTRY = re.compile(r'^    "([a-z0-9_\-]+)": "([^"]+)",$', re.M)


def fetch(name: str) -> str:
    with urllib.request.urlopen(URL.format(name=name), timeout=30) as r:
        svg = r.read().decode("utf-8")
    paths = re.findall(r'<path d="([^"]+)"', svg)
    if len(paths) != 1:
        raise SystemExit(f"{name}: ожидался один <path>, найдено {len(paths)}")
    return paths[0]


def main(names: list[str]) -> None:
    src = ICONS.read_text(encoding="utf-8")
    if PKG not in src:
        raise SystemExit(f"icons.py собран не из {PKG}")
    have = dict(ENTRY.findall(src))
    for n in names:
        if n not in have:
            try:
                have[n] = fetch(n)
                print("+", n)
            except urllib.error.HTTPError as e:
                print(f"! {n}: {e.code} — нет в {PKG}")
    body = "".join(f'    "{k}": "{v}",\n' for k, v in sorted(have.items()))
    start = src.index("ICON_PATHS: dict[str, str] = {\n") + len("ICON_PATHS: dict[str, str] = {\n")
    end = src.index("}\n", start)
    ICONS.write_text(src[:start] + body + src[end:], encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
