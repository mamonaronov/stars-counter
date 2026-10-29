#!/usr/bin/env bash
# Install the systemd unit, then build and start the bot container.
# --once / -o starts the container without installing a unit and without restart-on-boot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

SKIP_DOCKER=0
ONCE=0
UNIT=stars-counter.service
UNIT_PATH="/etc/systemd/system/$UNIT"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--skip-docker | --once | -o]

  deploy/$UNIT -> /etc/systemd/system/$UNIT
  docker compose up -d --build
    with docker-compose.yml only, if TELEGRAM_PROXY_URL is empty
    with docker-compose.proxy.yml too, if TELEGRAM_PROXY_URL is set
      (external Docker network telegram-proxy; start mihomo-proxy first)

Does not change .env. To update later: git pull --ff-only && ./deploy.sh

  --skip-docker   only install the systemd unit, do not touch the container
  --once, -o      start the container now without installing a systemd unit.
                  Docker restart policy is "no", so this container is not
                  started again after reboot. An already installed unit is
                  left in place; remove it with ./down.sh
EOF
}

for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=1 ;;
    --once|-o) ONCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "$ONCE" -eq 1 && "$SKIP_DOCKER" -eq 1 ]]; then
  echo "error: --once and --skip-docker cannot be used together" >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found" >&2; exit 1; }
}

need_cmd docker
if [[ "$ONCE" -eq 0 ]]; then
  need_cmd systemctl
  if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    echo "error: need root or sudo to install the systemd unit" >&2
    exit 1
  fi
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "==> creating .env from .env.example"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

bot_token="$(env_file_value BOT_TOKEN "$ROOT/.env")"
super_id="$(env_file_value SUPERADMIN_TELEGRAM_ID "$ROOT/.env")"
if [[ -z "$bot_token" || "$bot_token" == 1234567890:* ]]; then
  echo "error: fill BOT_TOKEN in $ROOT/.env before deploy" >&2
  exit 1
fi
if [[ -z "$super_id" || "$super_id" == "123456789" ]]; then
  echo "error: fill SUPERADMIN_TELEGRAM_ID in $ROOT/.env before deploy" >&2
  exit 1
fi

proxy_url="$(env_file_value TELEGRAM_PROXY_URL "$ROOT/.env")"
compose_extra=""
if [[ -n "$proxy_url" ]]; then
  compose_extra=" -f docker-compose.yml -f docker-compose.proxy.yml"
  echo "==> Telegram proxy: ${proxy_url}"
else
  echo "==> Telegram proxy: direct (TELEGRAM_PROXY_URL empty)"
fi

echo "==> ensuring data/ and backups/"
mkdir -p "$ROOT/data" "$ROOT/backups"

echo "==> git commit for image"
load_git_version
echo "    ${GIT_COMMIT} ${GIT_COMMIT_TITLE}"

compose_up() {
  local restart_policy="${1:-}"
  (
    cd "$ROOT"
    if [[ -n "$restart_policy" ]]; then
      export BOT_RESTART_POLICY="$restart_policy"
    fi
    if [[ -n "$proxy_url" ]]; then
      docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
    else
      docker compose up -d --build
    fi
  )
}

if [[ "$ONCE" -eq 0 ]]; then
  echo "==> installing $UNIT (WorkingDirectory=$ROOT)"
  docker_bin="$(command -v docker)"
  unit_tmp="$(mktemp)"
  awk -v wd="$ROOT" -v docker="$docker_bin" -v extra="$compose_extra" '
    /^WorkingDirectory=/ { print "WorkingDirectory=" wd; next }
    /^ExecStart=/ { print "ExecStart=" docker " compose" extra " up -d"; next }
    /^ExecStop=/ { print "ExecStop=" docker " compose" extra " stop"; next }
    { print }
  ' "$ROOT/deploy/$UNIT" > "$unit_tmp"
  run_sudo install -m 644 "$unit_tmp" "$UNIT_PATH"
  rm -f "$unit_tmp"

  echo "==> systemd daemon-reload"
  run_sudo systemctl daemon-reload
  run_sudo systemctl enable "$UNIT"
fi

if [[ -n "$proxy_url" ]]; then
  docker network create telegram-proxy 2>/dev/null || true
fi

if [[ "$SKIP_DOCKER" -eq 1 ]]; then
  echo "==> skip docker"
elif [[ "$ONCE" -eq 1 ]]; then
  echo "==> docker compose${compose_extra} up -d --build (restart policy: no)"
  compose_up no
  restart_policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' stars-counter-bot)"
  if [[ "$restart_policy" != "no" && "$restart_policy" != "" ]]; then
    echo "==> container restart policy is ${restart_policy}, forcing no"
    docker update --restart=no stars-counter-bot >/dev/null
  fi
else
  echo "==> docker compose${compose_extra} up -d --build"
  compose_up
  run_sudo systemctl start "$UNIT"
fi

echo
echo "done"
if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  echo "  container    $(docker inspect -f '{{.State.Status}}' stars-counter-bot 2>/dev/null || echo not-created)"
fi
if [[ "$ONCE" -eq 1 ]]; then
  echo "  boot         container restart policy no; systemd unit was not installed"
fi
echo "  logs         docker compose -f \"$ROOT/docker-compose.yml\" logs -f bot"
