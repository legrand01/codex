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

## Sustainable workload checkpoint — 2026-07-24

The first 24-hour attempt was deliberately rejected after a resource review:
the original eight-client unlimited lab drove the target PostgreSQL container
to 524.87% CPU. That is useful for a short stress test but not a sustainable
production-style soak, especially on a local staging host.

The staging overlay now requires a nonzero rate, defaults to two clients and
two transactions per second, caps target PostgreSQL at two CPUs and 2 GB, and
caps the generator at half a CPU and 128 MB. Under the bounded mixed workload:

- `pgbench` sustained 2.22 TPS with zero failed transactions and exercised both
  the weighted transaction and analytical scripts;
- a ten-sample resource profile measured target PostgreSQL at 5.17% mean and
  34.25% maximum CPU, versus 0.13% mean and 0.35% maximum for the generator;
- the Host Agent remained connected and all seven evidence types stayed fresh;
  and
- the measured rollback proof still distinguished the settings: median query
  execution was 106.4 ms with `work_mem=4MB` versus 155.6 ms at `64kB`, with
  median temporary writes increasing from 6,606 to 55,170 blocks. Rollback
  returned to 106.9 ms and the drill restored the exact pre-drill source.

## Automated release evidence

- Backend: 648 passed, 5 skipped.
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

The `7f39afd` local mechanics soak predates control-plane database-role
separation. It remains useful failure-drill evidence, but cannot qualify the
corrected release artifact.

1. Run the corrected least-privilege release candidate continuously for at
   least 24 hours in an isolated routable staging host using real TLS and a
   real paging webhook. Its `summary.json` must include a passing structured
   database-role verification plus an unchanged clean commit and immutable
   running-image manifest.
2. Copy a backup off-host and restore it on an independent PostgreSQL instance.
3. Burn down the explicitly captured strict typing debt. A full
   `mypy backend host_agent` currently reports 398 errors in 58 files.
   `mypy-baseline.json` records exact file, error-code, and normalized-message
   fingerprints under pinned mypy 1.19.1; CI rejects every unreviewed addition,
   removal, or change while new production staging modules remain strictly
   type-checked with no baseline.
4. Obtain a staffed go/no-go approval. Initial scope must remain one
   self-managed PostgreSQL target, reload-only settings, and human approval for
   every candidate. Provider-managed adapters and restart-context settings are
   excluded.

Use [the staging runbook](../ops/staging/README.md) for the qualification and
failure-drill commands.
