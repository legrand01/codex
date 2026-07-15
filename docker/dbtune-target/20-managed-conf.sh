#!/bin/sh
set -eu

mkdir -p "$PGDATA/conf.d"
chmod 700 "$PGDATA/conf.d"

if ! grep -Eq "^[[:space:]]*include_dir[[:space:]]*=?[[:space:]]*'conf.d'" "$PGDATA/postgresql.conf"; then
  {
    echo ""
    echo "# Postgres Tune Doctor lab baseline and deterministic late include."
    echo "work_mem = '64kB'"
    echo "include_dir = 'conf.d'"
  } >> "$PGDATA/postgresql.conf"
fi
