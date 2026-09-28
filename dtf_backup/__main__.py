"""Command line: python -m dtf_backup {app,serve,sync,render,status,check-api} ...

LDTF (`app`: tray + local site, or `serve`: console) is the main interface; the other commands are for scripts,
agents and automation. While LDTF is running, `sync` hands the job to it, so every sync on this computer shares
one network budget (DTF limits requests per IP); `--standalone` runs it in this process instead.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__
from .api import parse_user_ident
from .state import Archive
from .util import log, setup_logging

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIBRARY = APP_ROOT / "archive"


def archive_for(args: argparse.Namespace) -> Archive:
    library: Path = args.root
    if args.out:
        return Archive(args.out, args.out.resolve().parent)
    if not args.user:
        raise SystemExit("Укажите --user <ник> (или --out <папка архива>)")
    kind, value = parse_user_ident(args.user)
    if kind != "uri":  # the app names folders by nickname: find the archive of this user id first
        from .state import archive_dirs
        for d in archive_dirs(library):
            a = Archive(d, library)
            try:
                if a.exists() and str(a.get_meta("user_id") or "") == str(value):
                    return a
            finally:
                a.close()
    return Archive(library / (value if kind == "uri" else f"id{value}"), library)


GUARD_HINT = ("Защита архива остановила синхронизацию, архив не изменён. Посмотрите подробности в LDTF (вкладка "
              "«Синхронизация») или в `status`. Если материалы удалили вы сами или модерация, запустите "
              "sync --accept-deletions: пропавшее будет отмечено, в архиве останутся сохранённые версии.")


def delegate(running: dict, arch: Archive, args: argparse.Namespace) -> int:
    """Run the sync inside the running LDTF (shared network budget) and print its progress here."""
    base, token = running["url"], running["token"]

    def call(path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(base + path.lstrip("/"), data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-LDTF-Token": token, "Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    job = call("/api/jobs", {"nick": arch.nick, "kind": "sync", "full": args.full, "user": args.user,
                             "accept": args.accept_deletions})
    print(f"Синхронизация @{arch.nick} выполняется в запущенном LDTF (задание {job['id']}): {base}u/{arch.nick}/sync")
    last, cancelled = "", False
    while True:
        try:
            time.sleep(2)
            j = call(f"/api/jobs/{job['id']}")
        except KeyboardInterrupt:
            if not cancelled:
                cancelled = True
                call(f"/api/jobs/{job['id']}/cancel", {})
                print("Останавливаю… прогресс сохраняется")
            continue
        run = next((st for st in j["stages"] if st.get("status") == "running"), None)
        line = f"{j['state']}: {run['title']} {run.get('pct', '')}%" if run else j["state"]
        if line != last:
            print(line)
            last = line
        if j["state"] not in ("queued", "running"):
            if j.get("error"):
                print(f"Ошибка: {j['error']}")
            if j["state"] == "blocked":
                print(GUARD_HINT)
            return {"done": 0, "cancelled": 130, "blocked": 4}.get(j["state"], 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dtf-backup",
        description="LDTF (Local DTF): локальные копии профилей DTF — посты, комментарии с контекстом, медиа.")
    ap.add_argument("--version", action="version", version=f"dtf-backup {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, user: bool = True) -> None:
        p.add_argument("--root", type=Path, default=DEFAULT_LIBRARY, help="папка с архивами (по умолчанию ./archive)")
        if user:
            p.add_argument("--user", help="пользователь: ник, id или ссылка (petra, 136492, https://dtf.ru/petra)")
            p.add_argument("--out", type=Path, help="папка архива (по умолчанию <root>/<ник>)")
        p.add_argument("-v", "--verbose", action="store_true", help="подробный лог в консоль")

    pa = sub.add_parser("app", help="запустить LDTF со значком в трее (Windows) — основной способ работы")
    common(pa, user=False)
    pa.add_argument("--port", type=int, default=8765)
    pa.add_argument("--background", action="store_true", help="без открытия браузера (автозапуск с Windows)")

    pv = sub.add_parser("serve", help="запустить LDTF в консоли (без трея)")
    common(pv, user=False)
    pv.add_argument("--port", type=int, default=8765)
    pv.add_argument("--open", action="store_true", help="открыть браузер")
    pv.add_argument("--no-auto-sync", action="store_true", help="не запускать автосинхронизацию при старте")

    ps = sub.add_parser("sync", help="скачать или докачать данные (можно прерывать и продолжать)")
    common(ps)
    ps.add_argument("--workers", type=int, help="параллельных запросов к API (по умолчанию из настроек архива)")
    ps.add_argument("--rate", type=float, help="стартовый темп запросов к API в секунду")
    ps.add_argument("--media-workers", type=int, help="параллельных загрузок медиа")
    ps.add_argument("--refresh-days", type=int, help="перепроверять ответы и счётчики за последние N дней")
    ps.add_argument("--full", action="store_true", help="полная перепроверка (правки старых постов и комментариев)")
    ps.add_argument("--only", help="только стадии через запятую: posts,comments,threads,media")
    ps.add_argument("--no-media", action="store_true", help="не скачивать медиафайлы")
    ps.add_argument("--no-render", action="store_true", help="не пересобирать view.sqlite, md/ и data/ после sync")
    ps.add_argument("--accept-deletions", action="store_true",
                    help="продолжить после остановки защитой архива: пропавшее на DTF отметить, сохранённое оставить")
    ps.add_argument("--standalone", action="store_true",
                    help="работать в этом процессе, даже если LDTF запущен (иначе задание уходит в LDTF)")

    pr = sub.add_parser("render", help="пересобрать view.sqlite, md/ и data/ из локальных данных (без сети)")
    common(pr)
    pst = sub.add_parser("status", help="прогресс, ошибки, статистика архива")
    common(pst)
    pc = sub.add_parser("check-api", help="проверить, что API DTF отвечает так, как ожидает инструмент")
    pc.add_argument("--user", default="petra", help="профиль для проверок (по умолчанию petra)")
    pc.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "app":
        setup_logging(None, args.verbose, app_log=args.root / ".state" / "app.log")
        from .desktop import run_app
        return run_app(args.root, args.port, background=args.background)

    if args.cmd == "serve":
        setup_logging(None, args.verbose, app_log=args.root / ".state" / "app.log")
        from .web.server import serve
        serve(args.root, args.port, args.open, auto_sync=not args.no_auto_sync)
        return 0

    if args.cmd == "check-api":
        setup_logging(None, args.verbose)
        from .checkapi import main as check_main
        return check_main(args.user)

    arch = archive_for(args)
    setup_logging(arch.log_path if args.cmd != "status" else None, getattr(args, "verbose", False))

    if args.cmd == "status":
        from .sync import status
        print(status(arch))
        return 0

    if args.cmd == "sync" and not args.standalone:
        from .web.server import find_running
        running = find_running(args.root)
        if running and running.get("token"):
            ignored = [f for f in ("workers", "rate", "media_workers", "refresh_days", "only") if getattr(args, f)]
            if ignored or args.no_media or args.no_render:
                print("Ключи --workers/--rate/--media-workers/--refresh-days/--only/--no-media/--no-render "
                      "в запущенном LDTF не используются (там общий бюджет и настройки архива); "
                      "для них добавьте --standalone.")
            code = delegate(running, arch, args)
            arch.close()
            return code

    if args.cmd == "sync":
        from .sync import STAGES, Syncer
        only = None
        if args.only:
            only = [s.strip() for s in args.only.split(",") if s.strip()]
            bad = [s for s in only if s not in STAGES]
            if bad:
                raise SystemExit(f"Неизвестные стадии: {bad}; доступны: {', '.join(STAGES)}")
        s = arch.settings()
        log.info(f"Архив: {arch.root}")
        code = Syncer(arch, args.user, workers=args.workers or int(s["workers"]),
                      media_workers=args.media_workers or int(s["media_workers"]),
                      refresh_days=args.refresh_days if args.refresh_days is not None else int(s["refresh_days"]),
                      full=args.full, only=only, no_media=args.no_media or not s["media"],
                      rate=args.rate or float(s["rate"]), accept=args.accept_deletions).run()
        if code == 4:
            print(GUARD_HINT)
        if code in (0, 2) and not args.no_render:
            from .render import render
            render(arch)
        arch.close()
        return code

    if args.cmd == "render":
        from .render import render
        from .sync import lock_holder
        pid = lock_holder(arch.state_dir / "sync.lock")
        if pid:
            raise SystemExit(f"Архив сейчас синхронизируется (PID {pid}): дождитесь окончания, потом пересоберите.")
        render(arch)
        arch.close()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
