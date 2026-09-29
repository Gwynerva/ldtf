"""`render`: raw/ -> .state/view.sqlite (for the app) + data/ and md/ (for agents and reading without the app).

Fully offline and idempotent; runs after every sync and after updating the tool (new block renderers apply
to old data without re-downloading anything).
"""

from __future__ import annotations

import datetime as _dt
import shutil
import time
from typing import Callable

from . import __version__
from .blocks import Report
from .normalize import MediaResolver
from .reactions import Reactions
from .state import Archive
from .util import MSK, atomic_write_text, log, write_json
from .viewdb import Dataset, build_view, group_months

Progress = Callable[[str, int, int], None]

STUB = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Архив DTF · LDTF</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 16px}</style></head><body>
<h1>Архив DTF</h1>
<p>Этот архив открывается в приложении LDTF: запустите <b>LDTF.cmd</b> в папке приложения (на две папки выше).
Откроется браузер со всеми сохранёнными материалами, поиском и медиа.</p>
<p>Без приложения: тексты в Markdown лежат в папке <code>md/</code>,
данные для скриптов — в <code>data/</code> (описание — в <code>README.md</code>).</p>
</body></html>
"""


def render(arch: Archive, progress: Progress | None = None) -> dict:
    from .export import export
    t0 = time.time()
    if not arch.raw_profile().exists():
        raise SystemExit("Нет данных: архив ещё не синхронизирован.")
    step = progress or (lambda *_: None)
    report = Report()
    ds = Dataset(arch).load(step)
    resolver = MediaResolver.for_library(arch.library)
    groups = group_months(ds)
    log.info(f"[render] данные загружены: постов {len(ds.posts)}"
             + (f", комментариев пользователя {len(ds.my)}, веток контекста {len(ds.threads)}" if ds.comments else
                " (архив хранит только посты)"))
    build_view(ds, groups, resolver, step)
    log.info(f"[render] view.sqlite собрана ({arch.view_path.stat().st_size / 1e6:.0f} МБ)")
    rx = Reactions.for_archive(arch, resolver, report)
    res = export(ds, groups, resolver, rx, report, step)
    cleanup_legacy(arch)
    rep = {"renderedAt": _dt.datetime.now(MSK).isoformat(timespec="seconds"), "toolVersion": __version__,
           "unsupported": report.buckets["unsupported"], "generic": report.buckets["generic"],
           "errors": report.buckets["errors"], "unknownReactions": report.buckets["unknownReactions"],
           "context": res["context"], "counts": res["counts"]}
    write_json(arch.report_path, rep)
    log.info(f"[render] готово за {time.time() - t0:.0f} с")
    if report.buckets["unsupported"] or report.buckets["errors"]:
        log.info(f"[render] неподдерживаемые/ошибочные блоки: "
                 f"{ {k: v['count'] for k, v in {**report.buckets['unsupported'], **report.buckets['errors']}.items()} }")
    step("done", 1, 1)
    return rep


def cleanup_legacy(arch: Archive) -> None:
    """The static site of earlier versions is replaced by the app (it duplicated ~0.9 GB of HTML)."""
    for name in ("site", ".site.tmp", "site.old"):
        p = arch.root / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            log.info(f"[render] удалён устаревший статический сайт: {p.name}/")
    atomic_write_text(arch.root / "index.html", STUB)
