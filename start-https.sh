#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -f deploy/.env ] || { echo "Run sh start.sh and finish administrator setup first."; exit 1; }
docker compose --project-name yanwei-local --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.https.yml config --quiet
docker compose --project-name yanwei-local --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.https.yml up -d --build
