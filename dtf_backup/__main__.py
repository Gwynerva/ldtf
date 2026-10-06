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

from . import DEFAULT_PORT, __version__, config
from .api import parse_user_ident
from .guard import CLI_HINT
from .media import MEDIA_MODES
from .settings import CHOICES
from .state import Archive, archive_dirs, read_meta
from .util import human_bytes, log, setup_logging

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
        for d in archive_dirs(library):
            if str((read_meta(d, ("user_id",)) or {}).get("user_id") or "") == str(value):
                return Archive(d, library)
    return Archive(library / (value if kind == "uri" else f"id{value}"), library)


def set_scope(arch: Archive, scope: str, drop: bool) -> None:
    """`sync --scope`: remember what the archive keeps. Switching to posts only drops the comments it has - only
    with --drop-comments (the sync then drops them first, under its lock)."""
    from .scope import comment_footprint, comments_kept, describe, has_comment_data
    from .settings import save_settings
    s = arch.settings()
    if s["scope"] == scope:
        return
    if scope == "posts" and comments_kept(s) and has_comment_data(arch) and not drop:
        fp = comment_footprint(arch)
        raise SystemExit(f"В архиве сохранены {describe(fp)} (до {human_bytes(fp['bytes'])}). Архив «только посты» "
                         f"их не хранит: добавьте --drop-comments, чтобы удалить их и переключить архив.")
    arch.state_dir.mkdir(parents=True, exist_ok=True)
    save_settings(arch.settings_path, {**s, "scope": scope})
    print("Архив хранит " + ("только посты." if scope == "posts" else "посты и комментарии."))


def delegate(running: dict, arch: Archive, args: argparse.Namespace, kind: str = "sync") -> int:
    """Run the job inside the running LDTF (shared network budget) and print its progress here."""
    from .web.jobs import ACTIVE, BLOCKED, EXIT_CODES, RUNNING
    from .web.ui import Links
    base, token = running["url"], running["token"]

    def call(path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(base + path.lstrip("/"), data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-LDTF-Token": token, "Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    job = call("/api/jobs", {"nick": arch.nick, "kind": kind, "full": getattr(args, "full", False), "user": args.user,
                             "accept": getattr(args, "accept_deletions", False)})
    print(f"Синхронизация @{arch.nick} выполняется в запущенном LDTF (задание {job['id']}): {base}{Links(arch.nick).sync().lstrip('/')}")
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
        run = next((st for st in j["stages"] if st.get("status") == "running"), None) if j["state"] == RUNNING else None
        pct = f" {run['pct']:.0f}%" if run and run.get("pct") is not None else ""
        line = f"{j['state']}: {run['title']}{pct}" if run else j["state"]
        if line != last:
            print(line)
            last = line
        if j["state"] not in ACTIVE:
            if j.get("error"):
                print(f"Ошибка: {j['error']}")
            if j["state"] == BLOCKED:
                print(CLI_HINT)
            return EXIT_CODES.get(j["state"], 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dtf-backup",
        description="LDTF (Local DTF): локальные копии профилей DTF — посты, комментарии с контекстом, медиа.")
    ap.add_argument("--version", action="version", version=f"dtf-backup {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, user: bool = True) -> None:
        p.add_argument("--root", type=Path, default=None,
                       help="папка с архивами (по умолчанию ./archive или LDTF_ROOT)")
        if user:
            p.add_argument("--user", help="пользователь: ник, id или ссылка (petra, 136492, https://dtf.ru/petra)")
            p.add_argument("--out", type=Path, help="папка архива (по умолчанию <root>/<ник>)")
        p.add_argument("-v", "--verbose", action="store_true", help="подробный лог в консоль")

    pa = sub.add_parser("app", help="запустить LDTF со значком в трее (Windows) — основной способ работы")
    common(pa, user=False)
    pa.add_argument("--port", type=int, default=None,
                    help=f"порт (по умолчанию {DEFAULT_PORT} или следующий свободный; заданный явно — ровно он)")
    pa.add_argument("--background", action="store_true", help="без открытия браузера (автозапуск с Windows)")

    pv = sub.add_parser("serve", help="запустить LDTF в консоли (без трея)")
    common(pv, user=False)
    pv.add_argument("--host", default=None,
                    help="адрес (по умолчанию 127.0.0.1 — только этот компьютер; 0.0.0.0 — вся сеть, нужен пароль "
                         "LDTF_PASSWORD; можно задать LDTF_HOST)")
    pv.add_argument("--port", type=int, default=None,
                    help=f"порт (по умолчанию {DEFAULT_PORT} или следующий свободный; заданный явно или LDTF_PORT — ровно он)")
    pv.add_argument("--open", action="store_true", help="открыть браузер")
    pv.add_argument("--no-auto-sync", action="store_true", help="без синхронизаций по расписанию (только вручную)")

    ps = sub.add_parser("sync", help="скачать или докачать данные (можно прерывать и продолжать)")
    common(ps)
    ps.add_argument("--scope", choices=CHOICES["scope"],
                    help="что хранит архив (запоминается): all — посты и комментарии, posts — только посты")
    ps.add_argument("--drop-comments", action="store_true",
                    help="с --scope posts: удалить уже сохранённые комментарии архива")
    ps.add_argument("--workers", type=int, help="параллельных запросов к API (standalone; по умолчанию 4)")
    ps.add_argument("--rate", type=float, help="стартовый темп запросов к API в секунду (standalone; по умолчанию 10)")
    ps.add_argument("--media-workers", type=int, help="параллельных загрузок медиа (standalone; по умолчанию 8)")
    ps.add_argument("--refresh-days", type=int,
                    help="как далеко назад перепроверять ответы, счётчики и удалённые комментарии (по умолчанию 30 дней)")
    ps.add_argument("--full", action="store_true", help="проверить всё заново (правки старых постов и комментариев)")
    ps.add_argument("--only", help="только стадии через запятую: posts,comments,threads,media")
    ps.add_argument("--media", choices=MEDIA_MODES,
                    help="какие медиафайлы скачивать: all — все, posts — только из постов (с аватарками и реакциями, "
                         "без медиа комментариев), off — никакие (по умолчанию из настроек архива)")
    ps.add_argument("--no-media", action="store_true", help="не скачивать медиафайлы (то же, что --media off)")
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
    pc.add_argument("-v", "--verbose", action="store_true", help="подробный лог в консоль")

    args = ap.parse_args(argv)
    if hasattr(args, "root"):
        args.root = args.root or config.root() or DEFAULT_LIBRARY
    if hasattr(args, "port"):
        args.port = args.port or config.port()

    if args.cmd == "app":
        setup_logging(None, args.verbose, app_log=args.root / ".state" / "app.log")
        from .desktop import run_app
        return run_app(args.root, args.port, background=args.background)

    if args.cmd == "serve":
        setup_logging(None, args.verbose, app_log=args.root / ".state" / "app.log")
        from .web.server import serve
        return serve(args.root, args.port, args.open, auto_sync=not args.no_auto_sync,
                     host=args.host or config.host() or "127.0.0.1")

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

    if args.cmd == "sync" and args.scope:
        set_scope(arch, args.scope, args.drop_comments)

    if args.cmd == "sync" and not args.standalone:
        from .web.server import find_running
        running = find_running(args.root)
        if running and running.get("token"):
            ignored = [f for f in ("workers", "rate", "media_workers", "refresh_days", "only", "media") if getattr(args, f)]
            if ignored or args.no_media or args.no_render:
                print("Ключи --workers/--rate/--media-workers/--refresh-days/--only/--media/--no-media/--no-render "
                      "в запущенном LDTF не используются (там общий бюджет и настройки архива); "
                      "для них добавьте --standalone.")
            code = delegate(running, arch, args)
            arch.close()
            return code

    if args.cmd == "sync":
        from .sync import STAGES, Exit, Syncer
        only = None
        if args.only:
            only = [s.strip() for s in args.only.split(",") if s.strip()]
            bad = [s for s in only if s not in STAGES]
            if bad:
                raise SystemExit(f"Неизвестные стадии: {bad}; доступны: {', '.join(STAGES)}")
        log.info(f"Архив: {arch.root}")
        syncer = Syncer.from_settings(arch, args.user, arch.settings(), workers=args.workers, rate=args.rate,
                                      media_workers=args.media_workers, refresh_days=args.refresh_days,
                                      media="off" if args.no_media else args.media, full=args.full, only=only,
                                      accept=args.accept_deletions)
        code = syncer.run()
        if syncer.dropped:   # comments dropped (switched to posts only): free the files no archive needs now
            from .state import gc_media
            gc = gc_media(arch.library)
            if gc["status"] == "done":
                print(f"Из общего хранилища удалено файлов: {gc['files']}, освобождено {human_bytes(gc['bytes'])}.")
        if code == Exit.GUARD:
            print(CLI_HINT)
        if code in (Exit.OK, Exit.NETWORK) and not args.no_render:
            from .render import render
            render(arch)
        arch.close()
        return code

    if args.cmd == "render":
        from .render import render
        from .sync import lock_holder
        pid = lock_holder(arch.lock_path)
        if pid:
            raise SystemExit(f"Архив сейчас синхронизируется (PID {pid}): дождитесь окончания, потом пересоберите.")
        render(arch)
        arch.close()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
