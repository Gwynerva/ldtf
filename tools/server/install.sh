#!/bin/sh
# LDTF on a Linux server without Docker: systemd, Python 3.12+ (Debian 13, Ubuntu 24.04 and newer; on Debian 12 use
# Docker — its Python is 3.11). As root:
#
#   curl -fsSLO https://github.com/Gwynerva/ldtf/releases/latest/download/install.sh && sudo sh install.sh
#
#   sudo sh install.sh [--domain ldtf.example.com] [--port 8765] [--local] [LDTF-x.y.z-linux.tar.gz]
#   sudo sh /opt/ldtf/install.sh --update [LDTF-x.y.z-linux.tar.gz]   update (the app can do it by itself too)
#
# The app goes to /opt/ldtf, the archives to /var/lib/ldtf, the settings to /etc/ldtf/ldtf.env (the password of the
# web interface is made here and printed at the end); the service ldtf.service runs as the user ldtf.
# --domain: https on this domain (its DNS must point here) — Caddy in front of LDTF gets the certificate by itself;
#           LDTF then listens only on this server. --local: only on this server, for a proxy of your own.
set -eu

REPO="Gwynerva/ldtf"
APP=/opt/ldtf
DATA=/var/lib/ldtf
CONF=/etc/ldtf
PORT=8765
HOST=0.0.0.0
UPDATE=0
TARBALL=""
DOMAIN=""

die() { echo "LDTF: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --local) HOST=127.0.0.1; shift ;;
    --domain) DOMAIN="$2"; HOST=127.0.0.1; shift 2 ;;
    --update) UPDATE=1; shift ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    -*) die "неизвестный ключ $1 (см. --help)" ;;
    *) TARBALL="$1"; shift ;;
  esac
done

[ "$(id -u)" = 0 ] || die "нужны права root: sudo sh $0"
command -v systemctl >/dev/null 2>&1 || die "нужен systemd. Без него — Docker: https://github.com/$REPO#быстрый-старт"
command -v python3 >/dev/null 2>&1 || die "нужен Python 3.12+: apt install python3 (или Docker)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || die "нужен Python 3.12+, а здесь $(python3 -V 2>&1). На Debian 12 используйте Docker или Debian 13 / Ubuntu 24.04"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# the release: downloaded from GitHub (or given), checked against SHA256SUMS of the same release
python3 - "$TMP" "$REPO" "$TARBALL" <<'PY'
import hashlib, json, os, shutil, sys, urllib.request
tmp, repo, given = sys.argv[1], sys.argv[2], sys.argv[3]
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ldtf-install", "Accept": "application/octet-stream"})
    return urllib.request.urlopen(req, timeout=120).read()
if given:
    data = open(given, "rb").read()
    name = os.path.basename(given)
    sums_path = os.path.join(os.path.dirname(os.path.abspath(given)), "SHA256SUMS")
    sums = open(sums_path, encoding="utf-8").read() if os.path.exists(sums_path) else None
else:
    rel = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    name = next((n for n in assets if n.endswith("-linux.tar.gz")), None)
    if not name or "SHA256SUMS" not in assets:
        sys.exit(f"LDTF: в релизе {rel.get('tag_name')} нет архива для Linux")
    print(f"LDTF: скачиваю {name}")
    data, sums = get(assets[name]), get(assets["SHA256SUMS"]).decode()
digest = hashlib.sha256(data).hexdigest()
if sums is not None:
    want = next((l.split()[0] for l in sums.splitlines() if len(l.split()) == 2 and l.split()[1].lstrip("*") == name), None)
    if want != digest:
        sys.exit(f"LDTF: контрольная сумма {name} не совпала с SHA256SUMS — файл повреждён или подменён")
else:
    print(f"LDTF: рядом с {name} нет SHA256SUMS — проверка подлинности пропущена (sha256 {digest})")
open(os.path.join(tmp, "release.tar.gz"), "wb").write(data)
PY

mkdir -p "$TMP/x"
tar -xzf "$TMP/release.tar.gz" -C "$TMP/x"
NEW="$TMP/x/ldtf"
[ -f "$NEW/release.json" ] && [ -f "$NEW/dtf_backup/__init__.py" ] || die "это не архив релиза LDTF для Linux"
VER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$NEW/release.json")

id ldtf >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin ldtf
mkdir -p "$APP" "$DATA" "$CONF"

if [ "$UPDATE" = 1 ] || [ -f "$APP/release.json" ]; then
  [ -f "$APP/release.json" ] || die "LDTF не установлен в $APP — запустите без --update"
  OLD=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$APP/release.json")
  echo "LDTF: обновление $OLD → $VER"
  systemctl stop ldtf 2>/dev/null || true
  BK="$APP/.update/backup/$OLD"
  rm -rf "$BK"
  mkdir -p "$BK"
  for f in "$APP"/* "$APP"/.[!.]*; do
    [ -e "$f" ] || continue
    case "$(basename "$f")" in .update) continue ;; esac
    mv "$f" "$BK/"
  done
else
  echo "LDTF: установка $VER"
fi
cp -a "$NEW/." "$APP/"
chmod 755 "$APP/ldtf-run.sh" "$APP/install.sh"
chown -R ldtf:ldtf "$APP" "$DATA"

if [ ! -f "$CONF/ldtf.env" ]; then
  PASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
  cat > "$CONF/ldtf.env" <<EOF
# LDTF: settings of the service (systemctl restart ldtf after a change)
LDTF_ROOT=$DATA
LDTF_HOST=$HOST
LDTF_PORT=$PORT
# the password of the web interface
LDTF_PASSWORD=$PASS
# the schedule's "every day at 09:00" is in this time zone
TZ=Europe/Moscow
EOF
  chown root:ldtf "$CONF/ldtf.env"
  chmod 640 "$CONF/ldtf.env"
fi
if [ -n "$DOMAIN" ]; then   # behind Caddy: only Caddy talks to LDTF
  sed -i 's/^LDTF_HOST=.*/LDTF_HOST=127.0.0.1/' "$CONF/ldtf.env"
fi

install -m 644 "$APP/ldtf.service" /etc/systemd/system/ldtf.service
systemctl daemon-reload
systemctl enable ldtf >/dev/null 2>&1
systemctl restart ldtf

PORT=$(sed -n 's/^LDTF_PORT=//p' "$CONF/ldtf.env")
ok=0
for i in $(seq 1 30); do
  if python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2)" 2>/dev/null; then
    ok=1; break
  fi
  sleep 1
done
[ "$ok" = 1 ] || die "сервис не ответил за 30 секунд: journalctl -u ldtf -n 50"

if [ -n "$DOMAIN" ]; then   # https: Caddy (from the distribution) in front of LDTF
  if ! command -v caddy >/dev/null 2>&1; then
    { apt-get update -qq && apt-get install -y -qq caddy; } \
      || die "не удалось установить Caddy: установите его сами (https://caddyserver.com/docs/install)"
  fi
  CF=/etc/caddy/Caddyfile
  if [ ! -s "$CF" ] || grep -q '^:80 {' "$CF"; then          # the package's sample: replaced
    printf '%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT" > "$CF"
  elif ! grep -q "^$DOMAIN {" "$CF"; then                    # a Caddyfile of its own: LDTF's site added
    printf '\n%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT" >> "$CF"
  fi
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
LISTEN=$(sed -n 's/^LDTF_HOST=//p' "$CONF/ldtf.env")
echo
echo "LDTF $VER работает."
if [ -n "$DOMAIN" ]; then
  echo "  Адрес: https://$DOMAIN (сертификат Caddy получит сам за минуту, если DNS домена указывает сюда)"
elif [ "$LISTEN" = "127.0.0.1" ]; then
  echo "  Адрес: http://127.0.0.1:$PORT (только с этого сервера — для обратного прокси с https)"
else
  echo "  Адрес: http://${IP:-<адрес сервера>}:$PORT"
fi
[ -n "${PASS:-}" ] && echo "  Пароль: $PASS   (сменить: $CONF/ldtf.env, затем systemctl restart ldtf)"
echo "  Архивы: $DATA   Журнал: journalctl -u ldtf -f"
echo "  Обновления: «Настройки приложения → Обновления» или sudo sh $APP/install.sh --update"
if [ "$LISTEN" != "127.0.0.1" ]; then
  echo
  echo "  Пароль идёт по сети открытым текстом, пока перед LDTF нет https. С доменом:"
  echo "  sudo sh $APP/install.sh --update --domain ваш.домен   — https через Caddy одной командой."
fi
