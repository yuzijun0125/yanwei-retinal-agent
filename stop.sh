#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
docker compose --project-name yanwei-local --env-file deploy/.env -f deploy/compose.yml stop
