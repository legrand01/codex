#!/bin/sh
set -eu

clients="${PGBENCH_CLIENTS:-8}"
jobs="${PGBENCH_JOBS:-4}"
duration="${PGBENCH_DURATION_SECONDS:-60}"
progress="${PGBENCH_PROGRESS_SECONDS:-10}"
rate="${PGBENCH_RATE:-0}"

validate_integer() {
  value_name="$1"
  value="$2"
  case "$value" in
    ''|*[!0-9]*)
      echo "$value_name must be a non-negative integer" >&2
      exit 2
      ;;
  esac
}

validate_integer clients "$clients"
validate_integer jobs "$jobs"
validate_integer duration "$duration"
validate_integer progress "$progress"
validate_integer rate "$rate"
if [ "$clients" -lt 1 ] || [ "$jobs" -lt 1 ] || [ "$duration" -lt 1 ] || [ "$progress" -lt 1 ]; then
  echo "clients, jobs, duration, and progress must be greater than zero" >&2
  exit 2
fi
if [ "$jobs" -gt "$clients" ]; then
  echo "PGBENCH_JOBS cannot exceed PGBENCH_CLIENTS" >&2
  exit 2
fi

set -- \
  --no-vacuum \
  "--client=$clients" \
  "--jobs=$jobs" \
  "--time=$duration" \
  "--progress=$progress" \
  --file=/workload/transaction.sql@12 \
  --file=/workload/analytics.sql@1
if [ "$rate" -gt 0 ]; then
  set -- "$@" "--rate=$rate"
fi

echo "Starting mixed workload: clients=$clients jobs=$jobs rate=$rate/s"
while true; do
  pgbench "$@"
done
