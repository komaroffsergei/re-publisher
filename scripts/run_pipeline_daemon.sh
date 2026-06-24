#!/usr/bin/env bash
set -euo pipefail

SLEEP_SECONDS="${PIPELINE_DAEMON_SLEEP_SECONDS:-300}"
while true; do
  scripts/run_pipeline_once.sh || true
  sleep "$SLEEP_SECONDS"
done

