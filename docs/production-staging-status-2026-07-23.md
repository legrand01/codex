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

## Automated release evidence

- Backend: 631 passed, 5 skipped.
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
2. Prove Host Agent disconnection buffering and chronological replay.
3. Prove live duplicate-agent detection, critical alert delivery, write
   blocking, and resolution.
4. Prove measured-regression rollback against the transaction lab.
5. Copy a backup off-host and restore it on an independent PostgreSQL instance.
6. Resolve or explicitly baseline the existing strict typing debt: a full
   `mypy backend host_agent` currently reports 397 errors in 58 files. The CI
   gate currently type-checks only the new production staging modules.
7. Obtain a staffed go/no-go approval. Initial scope must remain one
   self-managed PostgreSQL target, reload-only settings, and human approval for
   every candidate. Provider-managed adapters and restart-context settings are
   excluded.

Use [the staging runbook](../ops/staging/README.md) for the qualification and
failure-drill commands.
