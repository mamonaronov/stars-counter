# Shared helpers for deploy.sh. Caller must set ROOT to the repository root.

load_git_version() {
  if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$ROOT" rev-parse --short HEAD)"
    GIT_COMMIT_TITLE="$(git -C "$ROOT" log -1 --first-parent --no-merges --pretty=%s)"
    if [[ -z "$GIT_COMMIT_TITLE" ]]; then
      GIT_COMMIT_TITLE="$(git -C "$ROOT" log -1 --pretty=%s)"
    fi
  else
    GIT_COMMIT="unknown"
    GIT_COMMIT_TITLE="unknown"
  fi
  export GIT_COMMIT GIT_COMMIT_TITLE
}

env_file_value() {
  local key="$1" file="$2"
  local val=""
  if [[ -f "$file" ]]; then
    val="$(awk -v k="$key" '
      index($0, k "=") == 1 {
        print substr($0, length(k) + 2)
        exit
      }
    ' "$file")"
  fi
  val="${val%$'\r'}"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  printf '%s' "$val"
}

run_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}
