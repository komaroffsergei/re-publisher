#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
ATTEMPTS="${ATTEMPTS:-40}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"

print_diagnostics() {
  echo "=== docker compose ps ===" >&2
  docker compose ps >&2 || true
  echo "=== web logs ===" >&2
  docker compose logs --tail=200 web >&2 || true
  echo "=== postgres logs ===" >&2
  docker compose logs --tail=120 postgres >&2 || true
  echo "=== full-cycle logs ===" >&2
  docker compose logs --tail=160 full-cycle >&2 || true
}

request() {
  local path="$1"
  curl -fsS --max-time 15 "${BASE_URL}${path}"
}

ok=0
for _ in $(seq 1 "$ATTEMPTS"); do
  if request "/health" >/tmp/re-publisher-health.json 2>/tmp/re-publisher-health.err; then
    ok=1
    break
  fi
  sleep "$SLEEP_SECONDS"
done

if [ "$ok" -ne 1 ]; then
  echo "Production healthcheck failed: ${BASE_URL}/health" >&2
  cat /tmp/re-publisher-health.err >&2 || true
  print_diagnostics
  exit 1
fi

echo "health=$(cat /tmp/re-publisher-health.json)"

if ! request "/api/pipeline/status" >/tmp/re-publisher-pipeline-status.json 2>/tmp/re-publisher-pipeline-status.err; then
  echo "Pipeline status endpoint failed: ${BASE_URL}/api/pipeline/status" >&2
  cat /tmp/re-publisher-pipeline-status.err >&2 || true
  print_diagnostics
  exit 1
fi

if ! request "/api/rewrite-worker" >/tmp/re-publisher-rewrite-worker.json 2>/tmp/re-publisher-rewrite-worker.err; then
  echo "Rewrite worker endpoint failed: ${BASE_URL}/api/rewrite-worker" >&2
  cat /tmp/re-publisher-rewrite-worker.err >&2 || true
  print_diagnostics
  exit 1
fi

echo "pipeline_status=$(cat /tmp/re-publisher-pipeline-status.json)"
echo "rewrite_worker=$(cat /tmp/re-publisher-rewrite-worker.json)"
