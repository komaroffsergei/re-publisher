#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/re-publisher}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"

git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

mkdir -p secrets sessions media models/active cache artifacts reports logs

docker compose build
docker compose up -d postgres
docker compose run --rm app python -m app.main migrate
docker compose up -d --remove-orphans app web full-cycle
docker compose exec -T postgres pg_isready -U max_collector -d max_collector
bash scripts/prod_smoke_check.sh

docker compose ps
