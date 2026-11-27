#!/bin/sh
# Hosts like Railway and Fly mount persistent volumes owned by root. Start as
# root, make sure the data volume belongs to the app user, then drop privileges.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /var/data
  # Only walk the volume when it isn't the app user's yet: with a big map
  # cache, chown -R on every boot can outlast the health check.
  if [ "$(stat -c %U /var/data)" != "appuser" ] || [ -n "$(find /var/data -maxdepth 2 ! -user appuser -print -quit 2>/dev/null)" ]; then
    chown -R appuser:appuser /var/data 2>/dev/null || true
  fi
  exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi
exec "$@"
