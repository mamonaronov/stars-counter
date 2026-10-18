#!/usr/bin/env bash
# Stop the service and remove the systemd unit installed by ./deploy.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

UNIT=stars-counter.service
UNIT_PATH="/etc/systemd/system/$UNIT"

usage() {
  cat <<EOF
Usage: $(basename "$0")

  systemctl disable --now $UNIT
  remove $UNIT_PATH
  systemctl daemon-reload

ExecStop of the unit runs docker compose stop. Data and backups are left in place.
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

need_cmd systemctl
if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
  echo "error: need root or sudo to remove the systemd unit" >&2
  exit 1
fi

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
