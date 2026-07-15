# DBTune P0 tuning lab

This lab is a disposable staging target. It is separate from the DBTune control
plane and starts with `work_mem=64kB` so the analytical workload spills to disk.
Eight `pgbench` clients continuously mix account-transfer transactions with a
large aggregation and sort.

Start it, including the authenticated Host Agent that owns the managed include:

```bash
docker compose --profile tuning-lab up -d \
  target-postgres target-workload target-host-agent
venv/bin/python scripts/benchmark_tuning_lab.py --runs 5
```

The benchmark reports the current `work_mem`, concurrent active sessions,
median execution time, temporary blocks read/written, and sort methods. Run it
before and after an approved P0 plan to compare the same query under load.

The full tuning-session path first records an immutable baseline, then proposes
one bounded candidate at a time. Approve each candidate from its session page.
The worker applies it, waits for the configured measurement window plus one
collector interval, measures the same fingerprint and safety metrics, and then:

- keeps the candidate only if it safely beats both baseline and best-so-far;
- restores the last verified best value when it regresses or is inconclusive;
- persists the score, deltas, coverage, variance, safety data, evidence links,
  decision, and rollback result for the session report.

Use the candidate history on the session Overview tab as the primary proof.
After the session completes, also query the target setting or rerun the
benchmark to confirm that the final live state matches the recorded best.

The session Configuration tab is the complete parameter ledger. It is resolved
from the target PostgreSQL major version and platform and shows all 15
reload-only entries (or 19 in restart-enabled mode), including current/source
provenance, allowlist state, baseline, best, pending value, pending restart, and
one final disposition per entry. The final report persists the same ordered
ledger so a rolled-back or unevaluated setting cannot disappear from history.

The target image sets the spill-heavy baseline in `postgresql.conf` and loads
`conf.d` after that baseline. It does not use `ALTER SYSTEM`. Approved managed
changes are written atomically to `conf.d/postgres_tune.conf`; the Configuration
tab records status, path, checksum, effective values, and rollback outcome.
Exact previous bytes are retained only in the control plane's private recovery
record and are redacted from API responses and reports.

Run the end-to-end managed-backend proof after the API and agent are ready:

```bash
venv/bin/python scripts/verify_managed_backend.py
```

The proof applies `work_mem=128kB` through the durable agent command channel,
verifies the managed file as the active source, then removes/restores that file
byte-for-byte and verifies the original `64kB` source again.

Emergency-reset the managed file to the image baseline without deleting data:

```bash
docker compose --profile tuning-lab exec target-postgres \
  sh -lc "rm -f /var/lib/postgresql/data/conf.d/postgres_tune.conf && \
  psql -U dbtune -d dbtune_target -c 'SELECT pg_reload_conf()'"
```

Local target DSN:

```text
postgresql://dbtune:dbtune-lab-only@127.0.0.1:55433/dbtune_target
```

The password is intentionally local-lab-only. Never reuse this configuration
for a production database.

Stop the disposable target and its workload when finished:

```bash
docker compose --profile tuning-lab stop \
  target-host-agent target-workload target-postgres
```
