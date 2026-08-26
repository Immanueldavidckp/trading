#!/usr/bin/env bash
#
# Server-side deploy for the trading backend on AWS Lightsail.
#
#   ./deploy.sh <commit-sha|branch|tag>
#
# The GitHub Actions job copies this script (plus healthcheck.sh and the
# rendered .env) to ~/.trading-deploy/ and runs it over SSH, so the version
# that runs is always the one from the commit being deployed.
#
# What it does, in order:
#   1. clone or fetch the repo
#   2. remember the currently deployed SHA
#   3. check out the target SHA, sync the venv, write .env + RELEASE
#   4. pm2 reload, then poll /api/health for the target SHA
#   5. if the health check fails, put the previous SHA back and exit non-zero
#
# Env overrides:
#   TRADING_DIR  checkout path            (default $HOME/trading)
#   REPO_URL     clone URL                (default the public GitHub repo)
#   ENV_FILE     rendered .env to install (default $HOME/.trading-deploy/env)
#   PM2_APP      pm2 process name         (default trading-backend)
#   HEALTH_URL / TIMEOUT   passed through to healthcheck.sh
set -euo pipefail

TARGET_REF="${1:?usage: deploy.sh <commit-sha|branch|tag>}"
TRADING_DIR="${TRADING_DIR:-$HOME/trading}"
REPO_URL="${REPO_URL:-https://github.com/Immanueldavidckp/trading.git}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/.trading-deploy}"
ENV_FILE="${ENV_FILE:-$DEPLOY_DIR/env}"
PM2_APP="${PM2_APP:-trading-backend}"
HEALTHCHECK="$DEPLOY_DIR/healthcheck.sh"

log() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

# requirements.txt needs python >= 3.12 (upstox-totp ships no older wheel), and
# the distro python3 is not always that new. Pick the newest one that qualifies.
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; }

pick_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python3 python3.12 python3.13; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null || continue
    if py_ok "$candidate"; then command -v "$candidate"; return 0; fi
  done
  echo "::error::no python >= 3.12 on this host (requirements.txt needs one; see deploy/server-setup.sh)" >&2
  return 1
}

# ── 1. Repo present? ────────────────────────────────────────────────────────
if [ ! -d "$TRADING_DIR/.git" ]; then
  log "First deploy — cloning into $TRADING_DIR"
  git clone "$REPO_URL" "$TRADING_DIR"
fi

cd "$TRADING_DIR"
git remote set-url origin "$REPO_URL"

log "Fetching origin"
git fetch --prune --tags origin

PREV_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
TARGET_SHA="$(git rev-parse --verify "${TARGET_REF}^{commit}")"
echo "previous : ${PREV_SHA:-<none>}"
echo "target   : $TARGET_SHA"

# ── The one release step, reused verbatim for a rollback ────────────────────
release() {
  local sha="$1"
  local backend="$TRADING_DIR/backend"
  local python

  # NOTE: this function is called as an `if` condition, which disables errexit
  # for everything inside it — so each step carries its own `|| return 1`.
  # Without that, a failed pip install would sail on into the pm2 reload and
  # release() would return the status of its last echo.

  log "Checking out ${sha:0:8}"
  # Hard checkout, not pull: the server is a deploy target, not a workspace, and
  # a locally modified tracked file (e.g. mock_portfolio.json rewritten by a
  # paper trade) must never be able to block a deploy. Untracked paths —
  # backend/.env, backend/local_data/, backend/.venv/ — are left alone.
  git -c advice.detachedHead=false checkout --force --detach "$sha" || return 1

  mkdir -p "$backend/local_data" "$backend/logs" || return 1

  log "Syncing Python venv"
  # Rebuild the venv when it is missing or built on a python that is now too
  # old, so an interpreter upgrade on the box doesn't leave a stale one behind.
  if [ ! -x "$backend/.venv/bin/python" ] || ! py_ok "$backend/.venv/bin/python"; then
    python="$(pick_python)" || return 1
    echo "creating venv with $python ($("$python" -V 2>&1))"
    rm -rf "$backend/.venv"
    "$python" -m venv "$backend/.venv" || return 1
  fi
  "$backend/.venv/bin/python" -m pip install --quiet --upgrade pip || return 1
  "$backend/.venv/bin/python" -m pip install --quiet --upgrade -r "$backend/requirements.txt" || {
    echo "::error::pip install failed — not touching the running app" >&2
    return 1
  }
  # Fail here rather than in a crash loop pm2 has to back off from.
  "$backend/.venv/bin/python" -c 'import uvicorn, fastapi, pandas' || {
    echo "::error::the venv cannot import its core dependencies" >&2
    return 1
  }

  # ── .env from the workflow's secrets, if one was staged ──
  if [ -f "$ENV_FILE" ]; then
    log "Installing .env from staged secrets"
    install -m 600 "$ENV_FILE" "$backend/.env" || return 1
  elif [ -f "$backend/.env" ]; then
    echo "keeping the .env already on the server"
  else
    echo "WARNING: no .env staged and none on disk — the app will start with defaults"
  fi

  # ── stamp the release so /api/health can prove which code is live ──
  cat > "$backend/local_data/RELEASE" <<RELEOF || return 1
commit=$sha
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ref=$TARGET_REF
RELEOF

  log "Reloading pm2 app $PM2_APP"
  cd "$backend" || return 1
  pm2 startOrReload ecosystem.config.js --update-env || return 1
  pm2 save --force || true

  log "Health check"
  HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}" \
  TIMEOUT="${TIMEOUT:-90}" \
    bash "$HEALTHCHECK" "$sha"
}

# ── 2. Release, roll back on a failed health check ──────────────────────────
if release "$TARGET_SHA"; then
  log "Deployed ${TARGET_SHA:0:8}"
  pm2 describe "$PM2_APP" | sed -n '1,20p' || true
  exit 0
fi

echo "::error::health check failed for ${TARGET_SHA:0:8}"
log "Last 40 log lines from $PM2_APP"
pm2 logs "$PM2_APP" --lines 40 --nostream || true

if [ -z "$PREV_SHA" ] || [ "$PREV_SHA" = "$TARGET_SHA" ]; then
  echo "::error::no previous release to roll back to — leaving the failed deploy in place"
  exit 1
fi

log "ROLLING BACK to ${PREV_SHA:0:8}"
if release "$PREV_SHA"; then
  echo "::error::rolled back to ${PREV_SHA:0:8}; ${TARGET_SHA:0:8} was not deployed"
else
  echo "::error::rollback to ${PREV_SHA:0:8} ALSO failed — the backend is down, log in and inspect"
fi
exit 1
