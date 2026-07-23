#!/bin/sh
set -eu

mkdir -p /backups

while true; do
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  partial="/backups/control-${timestamp}.dump.partial"
  final="/backups/control-${timestamp}.dump"
  pg_dump --format=custom --no-owner --no-privileges --file="$partial"
  mv "$partial" "$final"
  sha256sum "$final" > "${final}.sha256"
  find /backups -type f -mtime "+${BACKUP_RETENTION_DAYS}" -delete
  sleep "$BACKUP_INTERVAL_SECONDS"
done
