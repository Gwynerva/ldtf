"""The README banners and the social preview of the repository, drawn as HTML and photographed by headless Edge/Chrome.

    python tools/make_banners.py            # .github/assets/banner-{dark,light}.png (896×230 @2x), social-preview.png
    python tools/make_banners.py --html DIR # only the HTML, to look at in a browser

The pattern (DTF's wordmark and Material Symbols) comes from .github/assets/src/pattern-symbols.svg, the app icon from
dtf_backup/assets/brand/ldtf.svg, the chips' icons from dtf_backup/web/icons.py. Roboto is loaded from Google Fonts
(the render needs the network). The social preview is uploaded by hand: GitHub → Settings → Social preview.
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dtf_backup.web.icons import ICON_PATHS  # noqa: E402

ASSETS = ROOT / ".github" / "assets"
TAGLINE = "Полный офлайн-архив профиля: посты, комментарии со всего сайта, медиа"
CHIPS = [("article", "Посты"), ("forum", "Комментарии"), ("search", "Умный поиск"), ("photo_library", "Медиа у себя"),
         ("smart_toy", "MCP для ИИ")]
THEMES = {
    "dark": {"bg": ("#151A26", "#111318", "#0B0B0C"), "glow": ("#2A5FC8", ".42", ".08"), "ink": "#D9F0FF", "ink_op": "0.075",
             "title": "#FFFFFF", "sub": "#ADC6FF", "tag": "#C2C5CF", "chip": "#E2E2E9", "chip_bg": "rgba(217,240,255,.07)",
             "chip_line": "rgba(217,240,255,.18)", "chip_ic": "#ADC6FF",
             "icon_shadow": "drop-shadow(0 18px 40px rgba(42,95,200,.55))"},
    "light": {"bg": ("#E9F6FF", "#D9F0FF", "#C6E4FA"), "glow": ("#FFFFFF", ".85", ".2"), "ink": "#0B0B0C", "ink_op": "0.06",
              "title": "#0B0B0C", "sub": "#2A5FC8", "tag": "#3B4150", "chip": "#0B0B0C", "chip_bg": "rgba(255,255,255,.55)",
              "chip_line": "rgba(11,11,12,.12)", "chip_ic": "#2A5FC8",
              "icon_shadow": "drop-shadow(0 2px 3px rgba(11,11,12,.10)) drop-shadow(0 16px 36px rgba(42,95,200,.30))"},
}
# (CSS width, height, pattern scale, layout, pixels per CSS pixel): the social preview is 1280×640 (GitHub: under 1 MB)
SIZES = {"banner": (896, 230, 0.36, "row", 2), "social": (1280, 640, 0.52, "column", 1)}


def background(w: int, h: int, k: float, t: dict, glow: tuple[str, str] = ("30%", "45%")) -> str:
    """The gradient, a glow, and the tilted pattern under a radial fade (as the banners of 1.3 had it)."""
    symbols = (ASSETS / "src" / "pattern-symbols.svg").read_text(encoding="utf-8").split("\n", 1)[1]
    uses = []
    r = int(math.hypot(w, h) / k / 2) + 400
    row = 0
    for y in range(-r, r, 124):
        shift = -98 * (row % 4)
        for i, x in enumerate(range(-r + shift, r, 392)):
            ic = f"ic{(i + 3 * row) % 8}"
            uses.append(f'<use href="#{ic}" x="{x}" y="{y}"/><use href="#dtf" x="{x + 179.2:.1f}" y="{y + 12.6:.1f}"/>')
        row += 1
    b0, b1, b2 = t["bg"]
    g, g0, g1 = t["glow"]
    return (f'<svg class="bg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}"><defs>'
            f'<linearGradient id="bgG" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{b0}"/>'
            f'<stop offset=".55" stop-color="{b1}"/><stop offset="1" stop-color="{b2}"/></linearGradient>'
            f'<radialGradient id="glowG" cx="{glow[0]}" cy="{glow[1]}" r="55%"><stop offset="0" stop-color="{g}" stop-opacity="{g0}"/>'
            f'<stop offset=".6" stop-color="{g}" stop-opacity="{g1}"/><stop offset="1" stop-color="{g}" stop-opacity="0"/>'
            f'</radialGradient>{symbols}'
            f'<radialGradient id="fadeG" cx="50%" cy="50%" r="62%"><stop offset="0" stop-color="#fff" stop-opacity="0.14"/>'
            f'<stop offset=".55" stop-color="#fff" stop-opacity=".75"/><stop offset="1" stop-color="#fff"/></radialGradient>'
            f'<mask id="fadeM" maskUnits="userSpaceOnUse" x="0" y="0" width="{w}" height="{h}">'
            f'<rect width="{w}" height="{h}" fill="url(#fadeG)"/></mask></defs>'
            f'<rect width="{w}" height="{h}" fill="url(#bgG)"/><rect width="{w}" height="{h}" fill="url(#glowG)"/>'
            f'<g mask="url(#fadeM)"><g fill="{t["ink"]}" fill-opacity="{t["ink_op"]}" '
            f'transform="translate({w / 2} {h / 2}) rotate(-12) scale({k})">{"".join(uses)}</g></g></svg>')


def chip(name: str, label: str) -> str:
    return (f'<span class="chip"><svg viewBox="0 -960 960 960" aria-hidden="true"><path d="{ICON_PATHS[name]}"/></svg>'
            f'{label}</span>')


def page(kind: str, theme: str) -> str:
    w, h, k, layout, _ = SIZES[kind]
    t = THEMES[theme]
    icon = (ROOT / "dtf_backup" / "assets" / "brand" / "ldtf.svg").read_text(encoding="utf-8")
    icon = icon.replace("<svg ", '<svg class="icon" ', 1)
    row = layout == "row"
    css = f"""
  html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}
  body {{ position: relative; font-family: Roboto, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }}
  .bg {{ position: absolute; inset: 0; }}
  .hero {{ position: absolute; inset: 0; display: flex; align-items: center;
    {"gap: 34px; padding: 0 40px 0 46px;" if row else "flex-direction: column; justify-content: center; padding-bottom: 6px;"} }}
  .icon {{ flex: none; width: {132 if row else 148}px; height: {132 if row else 148}px; filter: {t["icon_shadow"]}; }}
  .text {{ display: flex; flex-direction: column; {"" if row else "align-items: center;"} min-width: 0; }}
  .head {{ display: flex; align-items: baseline; gap: 18px; {"" if row else "flex-direction: column; align-items: center; gap: 4px; margin-top: 22px;"} }}
  h1 {{ margin: 0; font-size: {64 if row else 104}px; line-height: 1; font-weight: 900; letter-spacing: -1.5px; color: {t["title"]}; }}
  .sub {{ font-size: {26 if row else 36}px; font-weight: 500; color: {t["sub"]}; letter-spacing: .2px; }}
  .tag {{ margin-top: {14 if row else 14}px; font-size: {15.5 if row else 22}px; color: {t["tag"]}; white-space: nowrap; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: {8 if row else 10}px; margin-top: {16 if row else 30}px; {"" if row else "justify-content: center; max-width: 1080px;"} }}
  .chip {{ display: flex; align-items: center; gap: {6 if row else 8}px; height: {30 if row else 42}px; padding: 0 {13 if row else 18}px 0 {10 if row else 14}px;
    border-radius: 999px; font-size: {13 if row else 18}px; font-weight: 500; color: {t["chip"]}; background: {t["chip_bg"]};
    box-shadow: inset 0 0 0 1px {t["chip_line"]}; white-space: nowrap; }}
  .chip svg {{ width: {16 if row else 22}px; height: {16 if row else 22}px; fill: {t["chip_ic"]}; flex: none; }}
"""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>LDTF banner</title>'
            f'<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=block" rel="stylesheet">'
            f'<style>{css}</style></head><body>{background(w, h, k, t, ("30%", "45%") if row else ("50%", "38%"))}<div class="hero">{icon}<div class="text">'
            f'<div class="head"><h1>LDTF</h1><span class="sub">Локальный DTF</span></div><div class="tag">{TAGLINE}</div>'
            f'<div class="chips">{"".join(chip(n, label) for n, label in CHIPS)}</div></div></div></body></html>')


def browser() -> str:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    for n in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge"):
        if shutil.which(n):
            return shutil.which(n)  # type: ignore[return-value]
    raise SystemExit("нужен Microsoft Edge, Chrome или Chromium")


def shoot(html: Path, png: Path, w: int, h: int, dpr: int = 2) -> None:
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--user-data-dir={prof}", f"--force-device-scale-factor={dpr}", f"--window-size={w},{h}",
                        "--virtual-time-budget=8000", f"--screenshot={png}", html.resolve().as_uri()],
                       check=True, capture_output=True, timeout=120)
    print(png.relative_to(ROOT) if png.is_relative_to(ROOT) else png)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--html", help="только записать HTML в эту папку")
    args = ap.parse_args()
    out = Path(args.html) if args.html else Path(tempfile.mkdtemp())
    out.mkdir(parents=True, exist_ok=True)
    jobs = [("banner", "dark", ASSETS / "banner-dark.png"), ("banner", "light", ASSETS / "banner-light.png"),
            ("social", "dark", ASSETS / "social-preview.png")]
    for kind, theme, png in jobs:
        html = out / f"{kind}-{theme}.html"
        html.write_text(page(kind, theme), encoding="utf-8")
        if not args.html:
            w, h, _, _, dpr = SIZES[kind]
            shoot(html, png, w, h, dpr)
    if not args.html:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
