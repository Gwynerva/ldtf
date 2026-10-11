#!/bin/sh
# The image starts as root only to give the archives' folder to the user LDTF runs as: a host folder Docker makes for
# the volume (./data in compose.yaml) belongs to root, and LDTF must not run as root. Then LDTF runs as
# LDTF_UID:LDTF_GID (1000:1000 by default). Started as another user (`user:` in compose, --user), it just starts.
set -e
DATA="${LDTF_ROOT:-/data}"
if [ "$(id -u)" = 0 ]; then
  uid="${LDTF_UID:-1000}"
  gid="${LDTF_GID:-1000}"
  mkdir -p "$DATA"
  if [ "$(stat -c %u "$DATA")" != "$uid" ] || [ "$(stat -c %g "$DATA")" != "$gid" ]; then
    echo "LDTF: папка $DATA передаётся пользователю $uid:$gid"
    chown -R "$uid:$gid" "$DATA"
  fi
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
fi
exec "$@"
