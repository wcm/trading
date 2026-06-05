#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  deploy/update-cloud.sh [options]

Update and restart the cloud paper-trading bot from GitHub.

Default behavior:
  - Checks the local git tree is clean and pushed to origin/main.
  - SSHes to the VPS.
  - Pulls latest origin/main in /opt/trading.
  - Rebuilds/restarts Docker Compose.
  - Prints container status and recent logs.

Options:
  --host HOST          SSH host or alias. Default: trading-bot-vps
  --remote-dir DIR    Repo path on VPS. Default: /opt/trading
  --branch BRANCH     Git branch to deploy. Default: main
  --no-build          Restart without Docker rebuild.
  --smoke             Run Docker smoke test before restart.
  --follow-logs       Follow logs after restart. Ctrl+C stops log viewing only.
  --tail-lines N      Lines of logs to print. Default: 120
  --skip-local-check  Do not check local git clean/pushed state.
  -h, --help          Show this help.

Examples:
  deploy/update-cloud.sh
  deploy/update-cloud.sh --host root@203.0.113.10
  deploy/update-cloud.sh --smoke --follow-logs
USAGE
}

host="${TRADING_BOT_VPS_HOST:-trading-bot-vps}"
remote_dir="${TRADING_BOT_REMOTE_DIR:-/opt/trading}"
branch="${TRADING_BOT_BRANCH:-main}"
run_build=1
run_smoke=0
follow_logs=0
tail_lines=120
skip_local_check=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      host="${2:-}"
      shift 2
      ;;
    --remote-dir)
      remote_dir="${2:-}"
      shift 2
      ;;
    --branch)
      branch="${2:-}"
      shift 2
      ;;
    --no-build)
      run_build=0
      shift
      ;;
    --smoke)
      run_smoke=1
      shift
      ;;
    --follow-logs)
      follow_logs=1
      shift
      ;;
    --tail-lines)
      tail_lines="${2:-}"
      shift 2
      ;;
    --skip-local-check)
      skip_local_check=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$host" || -z "$remote_dir" || -z "$branch" ]]; then
  echo "Host, remote dir, and branch must not be empty." >&2
  exit 2
fi

if ! [[ "$tail_lines" =~ ^[0-9]+$ ]]; then
  echo "--tail-lines must be a non-negative integer." >&2
  exit 2
fi

if [[ "$skip_local_check" -eq 0 ]] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Local working tree has uncommitted changes." >&2
    echo "Commit and push first, or rerun with --skip-local-check if you know why." >&2
    exit 1
  fi

  git fetch origin "$branch" >/dev/null
  local_head="$(git rev-parse "$branch")"
  remote_head="$(git rev-parse "origin/$branch")"
  if [[ "$local_head" != "$remote_head" ]]; then
    echo "Local $branch is not the same as origin/$branch." >&2
    echo "Push or pull first, then rerun this script." >&2
    exit 1
  fi
fi

printf 'Deploying %s to %s:%s\n' "$branch" "$host" "$remote_dir"

remote_dir_q="$(printf '%q' "$remote_dir")"
branch_q="$(printf '%q' "$branch")"

ssh "$host" \
  "REMOTE_DIR=$remote_dir_q BRANCH=$branch_q RUN_BUILD=$run_build RUN_SMOKE=$run_smoke FOLLOW_LOGS=$follow_logs TAIL_LINES=$tail_lines bash -s" <<'REMOTE'
set -euo pipefail

cd "$REMOTE_DIR"

echo "Remote repo: $(pwd)"
echo "Fetching origin/${BRANCH}"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [[ "$RUN_SMOKE" == "1" ]]; then
  echo "Running Docker smoke test"
  docker compose run --rm trading-bot \
    uv run --no-sync trading-bot smoke --check-alpaca --send-discord
fi

if [[ "$RUN_BUILD" == "1" ]]; then
  echo "Rebuilding and restarting Docker Compose"
  docker compose up -d --build
else
  echo "Restarting Docker Compose without rebuild"
  docker compose up -d
fi

docker compose ps

if [[ "$FOLLOW_LOGS" == "1" ]]; then
  docker compose logs --tail "$TAIL_LINES" -f trading-bot
else
  docker compose logs --tail "$TAIL_LINES" trading-bot
fi
REMOTE
