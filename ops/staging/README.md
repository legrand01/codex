# Production staging and soak runbook

Task 28 qualifies a release candidate; it does not enable production target
writes. The staging stack always sets both write switches to `false` and leaves
the confirmation empty.

## Environment boundary

Use an isolated host or VM with persistent Docker volumes, routable HTTPS, a
real alert receiver, and storage outside the host for copied backup artifacts.
Only TCP 443 should be exposed. The API, PostgreSQL, Redis, Prometheus, and
Alertmanager ports bind to loopback or the private Compose network.

`staging_init.py` generates four distinct PostgreSQL credentials:

- `dbtune_bootstrap` initializes the cluster and runs only the idempotent role
  provisioning job;
- `dbtune_migrator` owns the database, schema, tables, sequences, and functions;
- `dbtune_runtime` is the API/worker login and receives DML plus sequence access,
  but no object ownership, schema creation, migration-history writes, or audit
  mutation;
- `dbtune_backup` inherits `pg_read_all_data` and has no write privileges.

The role initializer runs before migrations, migrations run as
`dbtune_migrator`, and a post-migration verifier fails startup if ownership,
elevated role flags, runtime restrictions, or backup read-only status drift.
Never use `POSTGRES_USER` in `CONTROL_DATABASE_URL`, `MIGRATION_DATABASE_URL`,
or `BACKUP_DATABASE_URL`.

Initialize real staging:

```bash
venv/bin/python scripts/staging_init.py \
  --hostname staging.dbtune.example \
  --cert /secure/path/fullchain.pem \
  --key /secure/path/privkey.pem \
  --alert-webhook https://alerts.example/dbtune
venv/bin/python scripts/staging_preflight.py
bash scripts/staging_up.sh
```

For a disposable local mechanics check only:

```bash
venv/bin/python scripts/staging_init.py --local --force
bash scripts/staging_up.sh --local --with-lab
```

The local self-signed certificate and unreachable local alert receiver do not
satisfy the production staging gate.

The staging overlay rate-limits the mixed transaction/analytics lab to two
transactions per second with two clients, caps the target PostgreSQL container
at two CPUs and 2 GB, and caps the workload generator at half a CPU and 128 MB.
These defaults keep a long soak representative without turning host saturation
into the experiment. Set the `STAGING_PGBENCH_*` and
`STAGING_TARGET_*` values in `.env.staging` for a larger isolated host, but keep
the rate above zero; `staging_preflight.py` rejects an unlimited workload and
enforces the local ceiling.

## Target and soak

Register the tuning-lab target and provision its one-time agent token:

```bash
venv/bin/python scripts/provision_staging_lab.py \
  --base-url https://staging.dbtune.example
docker compose --env-file .env.staging \
  -f docker-compose.yml -f ops/staging/docker-compose.staging.yml \
  --profile tuning-lab up -d --force-recreate target-host-agent
```

Confirm its capabilities and evidence freshness in the UI before starting the
qualification clock.

Run the qualification for at least 24 hours and no more than 72:

```bash
venv/bin/python scripts/staging_preflight.py --require-target-agent
venv/bin/python scripts/staging_soak.py \
  --duration-hours 24 \
  --interval-seconds 30 \
  --base-url https://staging.dbtune.example \
  --output-dir artifacts/staging-soak/release-candidate
```

If the process or host is interrupted, rerun the same command with `--resume`.
The state and append-only event stream survive process restarts.

The automatic drills restart the worker, restart Redis, and create and restore
a control-plane backup into a disposable database. It also disconnects the
control plane while the Host Agent continues collecting, proves chronological
buffer replay, starts a duplicate agent and verifies the production write
interlock plus alert, and applies a deliberately regressive `work_mem` candidate
before proving byte-exact managed-file rollback. Readiness sampling runs in a
separate thread throughout the drills and cumulative samples/drill outcomes are
written atomically for safe `--resume`.

The local alert sink proves the Prometheus-to-Alertmanager webhook mechanics.
Production approval still requires evidence from the external paging receiver,
real public TLS, and a restore outside the staging host. Copy
`ops/staging/external-evidence.example.json` to a protected operator location,
fill it only with verifiable evidence identifiers, and pass it to the final
resume:

```bash
venv/bin/python scripts/staging_soak.py \
  --duration-hours 24 \
  --resume \
  --base-url https://staging.dbtune.example \
  --output-dir artifacts/staging-soak/release-candidate \
  --external-evidence /secure/path/external-evidence.json
```

## Go/no-go

`summary.json` may say `GO` only after the full qualification duration, at
least 99.5% successful readiness samples, ongoing target transaction progress,
at least 99.5% of the expected sampling cadence, no sampling gap longer than
three intervals (or 60 seconds, whichever is greater), disabled control-plane
write interlocks, a passing structured database-role verification, all
automatic drills passing, and all four external evidence gates. This prevents
host sleep, a stopped monitor, or a privileged runtime/backup credential from
being counted as successful soak time. It reports `PENDING_EXTERNAL_GATES`
rather than silently treating local alert or restore mechanics as production
proof.

Release remains **NO-GO** if any of the following are true:

- an unresolved critical alert, duplicate agent, stale worker lease, or failed rollback;
- restore has not been proven on an independent database;
- alert delivery has not been acknowledged;
- TLS, least-privilege credentials, or backup retention are placeholders;
- the API, worker, migration, backup, and cluster-bootstrap processes share a
  PostgreSQL login, or any non-bootstrap login is a superuser;
- the 24-hour minimum has not elapsed;
- provider-managed targets or restart-context changes are in the launch scope.

The initial launch scope is one self-managed PostgreSQL target, reload-only
parameters, explicit approval for every candidate, and production target writes
enabled only during a staffed change window after a separate go/no-go review.
