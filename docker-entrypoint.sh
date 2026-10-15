#!/bin/sh
set -eu

# Bind mounts ./data and ./backups are often created as root on the host.
# The app runs as botuser (uid 1000); SQLite cannot create files otherwise.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data /app/backups
  chown -R botuser:botuser /app/data /app/backups
  exec gosu botuser "$0" "$@"
fi

exec "$@"
