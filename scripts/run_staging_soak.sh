#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  echo "staging soak Python is not executable: $PYTHON" >&2
  exit 2
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  if ! command -v caffeinate >/dev/null 2>&1; then
    echo "caffeinate is required for a continuous macOS staging soak" >&2
    exit 2
  fi
  exec caffeinate -ims "$PYTHON" "$ROOT/scripts/staging_soak.py" "$@"
fi

exec "$PYTHON" "$ROOT/scripts/staging_soak.py" "$@"
