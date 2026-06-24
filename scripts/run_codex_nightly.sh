#!/usr/bin/env bash
set -euo pipefail

python -m app.content.nightly_maintainer export-codex-labeling-batch

if [ "${ENABLE_CODEX_NIGHTLY:-false}" != "true" ]; then
  echo "Codex nightly is disabled. Set ENABLE_CODEX_NIGHTLY=true to run the Codex CLI step."
  exit 0
fi

TODAY="$(date +%F)"
BATCH_DIR="artifacts/codex_labels/${TODAY}"
codex < config/codex_nightly_prompt.md

if [ -f "${BATCH_DIR}/auto_labeled_new_posts.jsonl" ]; then
  python -m app.content.nightly_maintainer import-codex-labels "${BATCH_DIR}/auto_labeled_new_posts.jsonl"
fi
