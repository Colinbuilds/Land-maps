#!/bin/sh
# Hosts like Railway and Fly mount persistent volumes owned by root. Start as
# root, make sure the data volume belongs to the app user, then drop privileges.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /var/data
  chown -R appuser:appuser /var/data 2>/dev/null || true
  exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi
exec "$@"
