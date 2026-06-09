#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  deploy/update-cloud.sh [options]

Update and restart the cloud paper-trading bot.

Default behavior:
  - Checks the local git tree is clean and pushed to origin/main.
  - SSHes to the VPS.
  - Tries to pull latest origin/main in /opt/trading.
  - If the VPS cannot pull GitHub directly, sends a local Git bundle and
    fast-forwards /opt/trading from that bundle.
  - Rebuilds/restarts Docker Compose.
  - Prints container status and recent logs.

Options:
  --host HOST          SSH host or alias. Default: trading-bot-vps
  --remote-dir DIR    Repo path on VPS. Default: /opt/trading
  --branch BRANCH     Git branch to deploy. Default: main
  --no-bundle-fallback
                       Fail instead of using a local Git bundle if remote pull fails.
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
bundle_fallback=1

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
    --no-bundle-fallback)
      bundle_fallback=0
      shift
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

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Run this script from inside the local trading bot git repo." >&2
  exit 1
fi

local_head="$(git rev-parse "$branch")"
local_short_head="$(git rev-parse --short "$branch")"

if [[ "$skip_local_check" -eq 0 ]] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Local working tree has uncommitted changes." >&2
    echo "Commit and push first, or rerun with --skip-local-check if you know why." >&2
    exit 1
  fi

  git fetch origin "$branch" >/dev/null
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

remote_git_update() {
  ssh "$host" \
    "REMOTE_DIR=$remote_dir_q BRANCH=$branch_q bash -s" <<'REMOTE'
set -euo pipefail

cd "$REMOTE_DIR"

echo "Remote repo: $(pwd)"
echo "Fetching origin/${BRANCH}"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
git status -sb
git log -1 --oneline
REMOTE
}

bundle_update() {
  safe_branch="$(printf '%s' "$branch" | tr -c '[:alnum:]._-' '-')"
  bundle_file="$(mktemp "${TMPDIR:-/tmp}/trading-bot-${safe_branch}-${local_short_head}.XXXXXX.bundle")"
  remote_bundle="/tmp/trading-bot-${safe_branch}-${local_short_head}.bundle"
  trap 'rm -f "$bundle_file"' EXIT

  echo "Creating local Git bundle for ${branch} at ${local_short_head}"
  git bundle create "$bundle_file" "$branch"

  echo "Copying bundle to ${host}:${remote_bundle}"
  scp "$bundle_file" "${host}:${remote_bundle}"

  remote_bundle_q="$(printf '%q' "$remote_bundle")"
  ssh "$host" \
    "REMOTE_DIR=$remote_dir_q BRANCH=$branch_q REMOTE_BUNDLE=$remote_bundle_q bash -s" <<'REMOTE'
set -euo pipefail

cd "$REMOTE_DIR"

echo "Remote repo: $(pwd)"
echo "Fetching ${BRANCH} from uploaded bundle"
git fetch "$REMOTE_BUNDLE" "$BRANCH:refs/remotes/origin/$BRANCH"
git checkout "$BRANCH"
git merge --ff-only "refs/remotes/origin/$BRANCH"
rm -f "$REMOTE_BUNDLE"
git status -sb
git log -1 --oneline
REMOTE
  rm -f "$bundle_file"
  trap - EXIT
}

restart_remote() {
  ssh "$host" \
    "REMOTE_DIR=$remote_dir_q RUN_BUILD=$run_build RUN_SMOKE=$run_smoke FOLLOW_LOGS=$follow_logs TAIL_LINES=$tail_lines bash -s" <<'REMOTE'
set -euo pipefail

cd "$REMOTE_DIR"

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
}

if remote_git_update; then
  echo "Remote GitHub pull succeeded"
elif [[ "$bundle_fallback" == "1" ]]; then
  echo "Remote GitHub pull failed; falling back to local Git bundle deploy"
  bundle_update
else
  echo "Remote GitHub pull failed and bundle fallback is disabled." >&2
  exit 1
fi

restart_remote
