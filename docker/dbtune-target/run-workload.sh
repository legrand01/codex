#!/bin/sh
set -eu

echo "Starting mixed transactional and analytical workload"
while true; do
  pgbench \
    --no-vacuum \
    --client=8 \
    --jobs=4 \
    --time=60 \
    --progress=10 \
    --file=/workload/transaction.sql@12 \
    --file=/workload/analytics.sql@1
done
