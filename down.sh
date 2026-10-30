#!/usr/bin/env bash
# Stop the bot container and remove the systemd unit installed by ./deploy.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

UNIT=stars-counter.service
UNIT_PATH="/etc/systemd/system/$UNIT"

usage() {
  cat <<EOF
Usage: $(basename "$0")

  docker compose stop
    with docker-compose.yml only, if TELEGRAM_PROXY_URL is empty
    with docker-compose.proxy.yml too, if TELEGRAM_PROXY_URL is set
  systemctl disable --now $UNIT   (if the unit is installed)
  remove $UNIT_PATH
  systemctl daemon-reload

Data and backups are left in place. The container is stopped even when
the unit was never installed (./deploy.sh --once).
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found" >&2; exit 1; }
}

need_cmd docker
need_cmd systemctl
if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
  echo "error: need root or sudo to remove the systemd unit" >&2
  exit 1
fi

proxy_url=""
if [[ -f "$ROOT/.env" ]]; then
  proxy_url="$(env_file_value TELEGRAM_PROXY_URL "$ROOT/.env")"
fi

echo "==> docker compose stop"
(
  cd "$ROOT"
  if [[ -n "$proxy_url" ]]; then
    docker compose -f docker-compose.yml -f docker-compose.proxy.yml stop
  else
    docker compose stop
  fi
)

if [[ -f "$UNIT_PATH" ]] || systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "==> stopping and disabling $UNIT"
  run_sudo systemctl disable --now "$UNIT"
  run_sudo systemctl reset-failed "$UNIT" || true
else
  echo "==> $UNIT is not installed"
fi

if [[ -f "$UNIT_PATH" ]]; then
  echo "==> removing $UNIT_PATH"
  run_sudo rm -f "$UNIT_PATH"
fi

echo "==> systemd daemon-reload"
run_sudo systemctl daemon-reload

echo
echo "done"
