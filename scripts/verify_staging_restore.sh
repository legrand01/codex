#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
source .env.staging
set +a

COMPOSE=(
  docker compose
  --env-file .env.staging
  -f docker-compose.yml
  -f ops/staging/docker-compose.staging.yml
)
OUTPUT_DIR="${1:-artifacts/staging-backups}"
mkdir -p "$OUTPUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$OUTPUT_DIR/control-${STAMP}.dump"
RESTORE_DB="dbtune_restore_${STAMP//[-:TZ]/}"

cleanup() {
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists --force "$RESTORE_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${COMPOSE[@]}" exec -T postgres pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format=custom \
  --no-owner \
  --no-privileges > "$DUMP"
shasum -a 256 "$DUMP" > "${DUMP}.sha256"

"${COMPOSE[@]}" exec -T postgres createdb \
  --username "$POSTGRES_USER" "$RESTORE_DB"
"${COMPOSE[@]}" exec -T postgres pg_restore \
  --username "$POSTGRES_USER" \
  --dbname "$RESTORE_DB" \
  --no-owner \
  --no-privileges < "$DUMP"

MIGRATIONS="$("${COMPOSE[@]}" exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$RESTORE_DB" \
  --tuples-only --no-align \
  --command "SELECT COUNT(*) FROM schema_migrations;")"
TABLES="$("${COMPOSE[@]}" exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$RESTORE_DB" \
  --tuples-only --no-align \
  --command "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public';")"

if [[ "${MIGRATIONS//[[:space:]]/}" -lt 19 || "${TABLES//[[:space:]]/}" -lt 10 ]]; then
  echo "Restore validation failed: migrations=$MIGRATIONS tables=$TABLES" >&2
  exit 1
fi
echo "Restore verified: dump=$DUMP migrations=$MIGRATIONS tables=$TABLES"
