#!/bin/sh
# LDTF для macOS/Linux: нужен Python 3.12+ (python3). Значка в трее здесь нет, приложение работает в терминале.
cd "$(dirname "$0")" || exit 1
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" -X utf8 -m dtf_backup serve --open "$@"
