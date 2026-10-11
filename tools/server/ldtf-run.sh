#!/bin/sh
# Starts LDTF as a server (systemd: ldtf.service; LDTF_ROOT, LDTF_HOST, LDTF_PORT, LDTF_PASSWORD come from
# /etc/ldtf/ldtf.env). After an update from the app (.update/pending: backup folder, old version, new version) a new
# version that fails to start twice in a row is rolled back to the old one, which then tells about it on its page.
APP="$(cd "$(dirname "$0")" && pwd)"
U="$APP/.update"
if [ -f "$U/pending" ]; then
  n=$(( $(cat "$U/attempts" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$U/attempts"
  if [ "$n" -gt 2 ]; then
    backup=$(sed -n 1p "$U/pending")
    old=$(sed -n 2p "$U/pending")
    new=$(sed -n 3p "$U/pending")
    if [ -d "$backup" ]; then
      for f in "$backup"/* "$backup"/.[!.]*; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        rm -rf "$APP/$name"
        mv "$f" "$APP/$name"
      done
      printf '{"ok": false, "from": "%s", "to": "%s", "error": "новая версия не запустилась"}\n' "$old" "$new" \
        > "$U/result.json"
      echo "LDTF: версия $new не запустилась — возвращена $old" >&2
    fi
    rm -f "$U/pending" "$U/attempts"
  fi
fi
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" -X utf8 -m dtf_backup serve "$@"
