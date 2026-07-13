# DBTune P0 tuning lab

This lab is a disposable staging target. It is separate from the DBTune control
plane and starts with `work_mem=64kB` so the analytical workload spills to disk.
Eight `pgbench` clients continuously mix account-transfer transactions with a
large aggregation and sort.

Start it:

```bash
docker compose --profile tuning-lab up -d target-postgres target-workload
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

Reset the setting to the spill-heavy baseline without deleting the sample data:

```bash
docker compose --profile tuning-lab exec target-postgres \
  psql -U dbtune -d dbtune_target \
  -c "ALTER SYSTEM SET work_mem = '64kB'" \
  -c "SELECT pg_reload_conf()"
```

Local target DSN:

```text
postgresql://dbtune:dbtune-lab-only@127.0.0.1:55433/dbtune_target
```

The password is intentionally local-lab-only. Never reuse this configuration
for a production database.

Stop the disposable target and its workload when finished:

```bash
docker compose --profile tuning-lab stop target-workload target-postgres
```
