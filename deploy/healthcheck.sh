#!/usr/bin/env bash
#
# Poll the backend's /api/health until it answers, optionally asserting that
# the running code is the commit we just deployed.
#
#   ./healthcheck.sh [expected-sha]
#
# Env:
#   HEALTH_URL  probe URL          (default http://127.0.0.1:8000/api/health)
#   TIMEOUT     seconds to wait    (default 90)
#
# Exit 0 = healthy, 1 = never came up / wrong commit.
set -uo pipefail

EXPECT_SHA="${1:-}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
TIMEOUT="${TIMEOUT:-90}"

deadline=$(( $(date +%s) + TIMEOUT ))
body=""
last=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  if body=$(curl -fsS --max-time 5 "$HEALTH_URL" 2>&1); then
    if [ -z "$EXPECT_SHA" ]; then
      echo "healthy: $body"
      exit 0
    fi
    # The deploy writes local_data/RELEASE; /api/health echoes it back. A stale
    # commit here means uvicorn is still serving the previous release.
    if printf '%s' "$body" | grep -Eq "\"commit\"[[:space:]]*:[[:space:]]*\"${EXPECT_SHA}\""; then
      echo "healthy at ${EXPECT_SHA:0:8}: $body"
      exit 0
    fi
    last="serving a different commit: $body"
  else
    last="$body"
  fi
  sleep 2
done

echo "UNHEALTHY after ${TIMEOUT}s — $HEALTH_URL"
echo "last response: ${last:-<no response>}"
exit 1
