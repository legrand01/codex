# P0 production release runbook

P0 permits only allowlisted PostgreSQL settings that pass backend-specific live
preflight and snapshot validation. Reload-context settings may become active
after verified `pg_reload_conf()`; restart-context settings remain explicitly
pending until an authorized restart. Arbitrary SQL, index DDL, replicas, stale
snapshots, missing verification evidence, and unverified rollback state fail
closed.

## 1. Provision the control plane

Set secrets in the deployment secret manager, not in the Compose file:

```text
ENVIRONMENT=production
AUTH_REQUIRED=true
AGENT_AUTH_REQUIRED=true
BOOTSTRAP_ADMIN_TOKEN=<at least 32 random characters; remove after provisioning>
DATABASE_URL=<TLS control-plane PostgreSQL DSN>
REDIS_URL=<authenticated/TLS Redis URL>
EVIDENCE_CLEANUP_ENABLED=true
EVIDENCE_RAW_RETENTION_DAYS=30
EVIDENCE_REFERENCED_RETENTION_DAYS=90
EVIDENCE_ROLLUP_RETENTION_DAYS=365
WRITE_EXECUTION_ENABLED=false
PRODUCTION_WRITE_ENABLED=false
PRODUCTION_WRITE_CONFIRMATION=
```

Run migrations before starting the API and worker:

```bash
venv/bin/python -m backend.db.init_db
venv/bin/python scripts/create_api_principal.py \
  --subject release-admin --display-name "Release Admin" --role admin
```

The principal command prints the API token once. Store it in the secret manager.
Production startup aborts if the database or identity configuration cannot be
validated.

Run migrations with a separate schema-owner credential. The API and worker
credential should have only DML and sequence privileges on the application
schema and must not own `audit_log` or be able to drop its append-only rules.
The bundled staging path enforces this using `dbtune_bootstrap`,
`dbtune_migrator`, `dbtune_runtime`, and `dbtune_backup`; real deployments may
use different role names but must preserve the same privilege boundaries.

## 2. Provision each target with least privilege

Use a distinct login per target. Grant only the parameters approved for that
host; repeat the final statement for each allowlisted parameter:

```sql
CREATE ROLE dbtune_agent LOGIN PASSWORD '<secret>';
GRANT pg_read_all_settings TO dbtune_agent;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_reload_conf() TO dbtune_agent;
GRANT ALTER SYSTEM ON PARAMETER work_mem TO dbtune_agent;
```

Store its TLS DSN under a target-specific environment variable such as
`DBTUNE_TARGET_ACME_PRIMARY_DSN`. Configure the host with only that environment
variable name; the DSN itself is never stored in the control-plane database.
Production target DSNs must use `sslmode=require`, `verify-ca`, or `verify-full`.

Rotate an agent token through `POST /api/v1/fleet/{host_id}/agent-token`, then
deploy the returned one-time token as `AGENT_TOKEN`. Persist
`/var/lib/dbtune-agent` so evidence survives agent restarts and network outages.
The agent probes and reports tuning capabilities at startup and on every
heartbeat. Keep `RESTART_CAPABILITY`, `PROVIDER_API_CAPABILITY`, and
`MANAGED_FILE_ACCESS` false unless that capability has been explicitly installed,
tested, and enrolled for the target; connectivity alone never enables them.

For an enrolled self-managed host, set an absolute `MANAGED_CONF_PATH` ending
in `conf.d/postgres_tune.conf`. Provision `postgresql.conf` with a deterministic
late `include_dir = 'conf.d'`, and give the Host Agent access to that directory.
The agent verifies include ordering, same-filesystem atomic replacement,
ownership, mode, free space, `pg_file_settings`, and reload permission before
advertising the capability. It rejects command-line, `postgresql.auto.conf`,
later-include, database/user, or provider-owned conflicts. Do not grant the
control-plane process direct access to the target filesystem.

Managed cloud hosts must select `configuration_backend=provider` and have an
explicit adapter for their platform. Missing adapters fail closed; never enable
`MANAGED_FILE_ACCESS` to simulate a provider parameter group.

## 3. Release with writes disabled

Start the API, worker, frontend, Redis, and control-plane PostgreSQL. Verify:

```bash
docker compose config -q
venv/bin/ruff check backend host_agent tests
venv/bin/python -m pytest -q
npm --prefix frontend run build
RUN_P0_TARGET_INTEGRATION=1 venv/bin/python -m pytest -q tests/test_p0_target_integration.py -s
```

Confirm authenticated HTTP and WebSocket access, tenant isolation, host
heartbeats, fresh evidence, worker lease heartbeats, and an approved dry-run on a
staging primary. The Start tuning preflight must show a fresh capability report,
zero blockers, and at least one independently allowlisted parameter.

## 4. Enable a production target deliberately

All three interlocks are required:

1. Set the host execution policy to `writes_enabled=true`, environment
   `production`, and the target DSN environment-variable name.
2. Set `WRITE_EXECUTION_ENABLED=true` and restart the worker.
3. Set `PRODUCTION_WRITE_ENABLED=true` and
   `PRODUCTION_WRITE_CONFIRMATION=PRODUCTION_WRITES_AUTHORIZED`.

Disable either global switch to stop new target mutations. Do not terminate a
worker while a write operation is `in_progress`; if a process crashes, the next
worker reconciles the live target state and either records the verified apply,
retries an untouched operation, or rolls back a partial state.

## 5. Rollback and incident response

- Use the rollback API for an applied plan; rollback restores the captured
  pre-change provenance and verifies the result on the target.
- Managed-file rollback restores the exact prior bytes, owner, mode, or prior
  absence, then reloads and verifies value plus sourcefile. Exact bytes remain
  private recovery data and must not appear in APIs, reports, logs, or events.
- If verification evidence is absent, stale, or degraded beyond the configured
  threshold, the orchestrator rolls back automatically.
- Preserve `audit_log`, `write_operations`, `run_jobs`, the run report, and agent
  buffered evidence for incident review.
- Turn both global write switches off before investigating unexpected behavior.

## 6. Evidence lifecycle operations

Raw evidence is retained for 30 days by default. Snapshots referenced by plans,
baselines, advisories, candidates, or workload fingerprints remain raw for 90
days, matching the report window. Before cleanup removes an eligible payload it
writes a tenant/host/run/type/day rollup in the same transaction. Rollups remain
for 365 days by default.

Preview the exact eligible footprint before manual maintenance:

```bash
venv/bin/python scripts/evidence_maintenance.py
```

Execute bounded cleanup only after reviewing the preview:

```bash
venv/bin/python scripts/evidence_maintenance.py --execute
```

The worker performs the same tenant-scoped cleanup every
`EVIDENCE_CLEANUP_INTERVAL_SECONDS`. Concurrent jobs for one tenant are rejected
by an advisory lock. Monitor `EVIDENCE_RETENTION_COMPLETED` and
`EVIDENCE_RETENTION_FAILED` events and the Evidence tab lifecycle panel. A failed
batch rolls back both its rollup writes and raw deletions. Upgraded deployments
backfill persisted payload sizes in bounded batches; the lifecycle panel reports
how many older snapshots remain unmeasured while this completes.
