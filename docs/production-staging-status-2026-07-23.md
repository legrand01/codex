# Production staging status — 2026-07-23

## Decision

**PENDING QUALIFICATION — not approved for production target writes.**

The release candidate is ready for a real 24-72 hour staging soak. The local
production-like mechanics checkpoint passed, but it intentionally cannot
produce a `GO` decision before the 24-hour minimum and the external operator
drills complete.

## Verified in the live local staging stack

- Production security validation passed with generated non-default control
  database, Redis, and administrator secrets.
- TLS ingress served dependency-aware readiness; PostgreSQL and authenticated
  Redis both reported up.
- The one-shot migration container applied all 19 migrations before API and
  worker startup.
- API and worker container health checks passed independently.
- Prometheus scraped the control plane successfully and loaded six alert rules.
- The authenticated tuning-lab agent connected and delivered all seven evidence
  categories.
- The mixed `pgbench` workload remained active throughout a 144-second mechanics
  soak.
- Readiness succeeded for 43 of 43 samples (100%).
- Ledger rows increased from 108,718 to 172,084 (+63,366).
- Worker restart passed with 10.239 seconds recorded recovery.
- Authenticated Redis restart passed with 0.365 seconds recorded recovery.
- Backup/restore passed in 0.917 seconds; the restored database contained all 19
  migrations and 31 public tables.
- The durable decision was correctly `PENDING_QUALIFICATION`, not `GO`.

The local soak artifacts are intentionally ignored at
`artifacts/staging-soak/local-mechanics/`.

## Strengthened failure-drill checkpoint — 2026-07-24

The release harness now automates and durably records all six staging drills
while readiness and transaction sampling continue in a separate thread:

- worker restart and recovery;
- authenticated Redis restart and recovery;
- Host Agent disconnection, local buffering, chronological replay, and drain;
- duplicate-agent detection, target-write blocking, Prometheus alert firing,
  local Alertmanager webhook delivery, and lease resolution;
- a controlled `work_mem` regression applied through the real managed-file
  backend, measured rollback to the control value, and exact restoration of the
  pre-drill value, source, and source file; and
- backup creation and restore verification on a disposable database.

A compressed orchestration run completed all six drills successfully. The
transaction workload advanced from 740,222 to 827,668 (+87,446), the evidence
buffer replayed with zero chronological inversions, and the managed-file
regression and rollback completed successfully. In the preceding isolated live
regression proof, median execution time changed from 383.5 ms at `4MB` to
1,437.0 ms at `64kB` before rollback.

The compressed run correctly returned `NO_GO`: 128 of 130 readiness samples
passed (98.46%) because two samples captured the deliberate API outage, the
24-hour minimum had not elapsed, and no external operator evidence was
supplied. The production threshold remains 99.5%; it was not weakened to make a
short mechanics test appear qualified. The harness also requires at least 99.5%
of the expected sampling cadence and a bounded maximum gap, so host sleep or a
stopped monitor cannot be counted toward qualification.

## Automated release evidence

- Backend: 633 passed, 5 skipped.
- Ruff: passed.
- Strict type checking for the new staging/release modules: passed.
- Frontend lint and production build: passed.
- Frontend production dependency audit: zero known vulnerabilities.
- Pinned Python 3.11 production runtime lock: zero known vulnerabilities in a
  no-resolution audit.
- Development and staging Compose models: valid.
- Backend images rebuilt successfully from the pinned runtime lock.
- Runtime base and service images use immutable manifest digests.

## Remaining production blockers

1. Run the same release candidate continuously for at least 24 hours in an
   isolated routable staging host using real TLS and a real paging webhook.
2. Copy a backup off-host and restore it on an independent PostgreSQL instance.
3. Resolve or explicitly baseline the existing strict typing debt: a full
   `mypy backend host_agent` currently reports 397 errors in 58 files. The CI
   gate currently type-checks only the new production staging modules.
4. Obtain a staffed go/no-go approval. Initial scope must remain one
   self-managed PostgreSQL target, reload-only settings, and human approval for
   every candidate. Provider-managed adapters and restart-context settings are
   excluded.

Use [the staging runbook](../ops/staging/README.md) for the qualification and
failure-drill commands.
