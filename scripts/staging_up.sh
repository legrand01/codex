#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ALLOW_LOCAL=()
LAB_PROFILE=()
LOCAL_PROFILE=()
if [[ "${1:-}" == "--local" ]]; then
  ALLOW_LOCAL=(--allow-local)
  LOCAL_PROFILE=(--profile local-observability)
  shift
fi
if [[ "${1:-}" == "--with-lab" ]]; then
  LAB_PROFILE=(--profile tuning-lab)
  shift
fi

venv/bin/python scripts/staging_preflight.py "${ALLOW_LOCAL[@]}"
docker compose \
  --env-file .env.staging \
  -f docker-compose.yml \
  -f ops/staging/docker-compose.staging.yml \
  --profile observability \
  --profile operations \
  "${LOCAL_PROFILE[@]}" \
  "${LAB_PROFILE[@]}" \
  up -d --build

HTTPS_PORT="$(awk -F= '$1 == "STAGING_HTTPS_PORT" {print $2}' .env.staging)"
for _ in {1..60}; do
  if curl --insecure --fail --silent \
      "https://127.0.0.1:${HTTPS_PORT:-18443}/health/ready" >/dev/null; then
    echo "Staging is ready at https://127.0.0.1:${HTTPS_PORT:-18443}"
    exit 0
  fi
  sleep 2
done

docker compose \
  --env-file .env.staging \
  -f docker-compose.yml \
  -f ops/staging/docker-compose.staging.yml \
  ps
echo "Staging did not become ready within 120 seconds" >&2
exit 1
