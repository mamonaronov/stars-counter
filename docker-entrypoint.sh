#!/bin/sh
set -eu

# Bind mounts ./data and ./backups are often created as root on the host.
# The app runs as botuser (uid 1000); SQLite cannot create files otherwise.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data /app/backups
  chown -R botuser:botuser /app/data /app/backups
  # The socket is usually root:docker 660. Join that group so the bot can read logs.
  if [ -S /var/run/docker.sock ]; then
    sock_gid="$(stat -c '%g' /var/run/docker.sock)"
    if [ "$sock_gid" != "0" ]; then
      sock_group="$(getent group "$sock_gid" | cut -d: -f1 || true)"
      if [ -z "$sock_group" ]; then
        sock_group=dockerhost
        groupadd --gid "$sock_gid" "$sock_group"
      fi
      usermod -aG "$sock_group" botuser
    fi
  fi
  exec gosu botuser "$0" "$@"
fi

exec "$@"
