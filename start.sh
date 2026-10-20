#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
docker info >/dev/null
docker compose version
IMAGE=${PYTHON_IMAGE:-docker.m.daocloud.io/library/python:3.11-slim}
if [ ! -f deploy/.env ]; then
  docker pull "$IMAGE"
  docker run --rm -v "$ROOT:/app" -w /app -e "PYTHON_IMAGE=$IMAGE" "$IMAGE" python integration/deployment.py init
fi
docker compose --project-name yanwei-local --env-file deploy/.env -f deploy/compose.yml config --quiet
docker compose --project-name yanwei-local --env-file deploy/.env -f deploy/compose.yml up -d --build
printf '\nOpen http://localhost:18880/hosting-admin for administrator setup; visitors use port 18881.\n'
