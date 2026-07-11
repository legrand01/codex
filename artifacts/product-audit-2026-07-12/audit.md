# DBTune workflow and configuration ownership audit

## Verdict

The current product splits one tuning session across active-only queues and
UUID-driven lookup screens. Replace this with a persistent tuning-session
workspace. Runs, plans, evidence, changes, verification, rollback, audit, and
the final report should all share one selected `run_id`.

The authenticated DBTune product was subsequently inspected in ChatGPT Atlas.
Its live reference captures are stored in
`docs/postgres-dba-demo-assets/dbtune-live-2026-07-12/` and were used to update
the Kiro requirements, design, and tasks specifications.

## Confirmed behavior

1. The Runs API filters to queued, running, waiting-approval, and unresponsive
   states. Completed run `8de7c2fa-6817-45be-8a7d-c55c3a27ade6` therefore does
   not appear even though its detail endpoint and report still exist.
2. Plans is a pending-approval queue, not plan history.
3. Evidence and Reports require users to paste a run UUID.
4. There is no UI entry point for creating a tuning run.
5. The planner is currently a small goal-keyword/rule system. It does not run a
   candidate-configuration optimization loop against a stable workload
   objective.

## Recommended information architecture

- Primary navigation: Fleet, Tuning, Reports, Administration.
- Tuning landing page: persistent session history plus `Start tuning`.
- Session route: `/tuning/:runId`.
- Session tabs: Overview, Configuration, Workload, Evidence, Activity, Report.
- Persistent session header: host, target metric, mode, status, baseline, best
  result, current candidate, start/end time, and safe actions.

## Live DBTune baseline functions

- Dashboard: tuning-session selector, AQR chart, Workload Fingerprint selector,
  and time ranges.
- Tuning: Workload Fingerprint or system-wide mode, recommended/manual query
  selection, low-coverage warning, human-in-the-loop toggle, reload-only versus
  restart-enabled mode, parameter selection, and AQR/TPS/fingerprint
  guardrails.
- Fingerprints: named custom fingerprint builder using query, AQR, calls, total
  duration, runtime coverage, and last seen.
- Configuration history: active version, parameter values/units, search,
  compare, download, and guarded apply.
- Event logs: time, severity, event code, and free-text filters; duplicate-agent
  detected/resolved codes were visible in the live account.
- Agent: independent capability indicators and version-specific setup for
  pg_stat_statements, pg_monitor, reload-only grants, and restart-mode grants.

The live supported parameter selector showed the same 15 reload-only and four
restart-enabled settings documented by DBTune. The updated specification
requires a final disposition for every supported setting rather than reporting
only the one that changed.

## Tuning workflow

1. Choose host and target: recommended/custom workload fingerprint, AQR, TPS,
   or a combined objective.
2. Choose reload-only or restart-enabled mode.
3. Run preflight and capture the baseline configuration, workload, OS metrics,
   query statistics, and configuration provenance.
4. Evaluate multiple bounded candidate configurations against the same
   objective, including warmup and measurement windows.
5. Compare each candidate with baseline and best-so-far; require approval at
   configured gates.
6. Keep the best verified configuration or restore the exact baseline.
7. Report every supported setting as changed, retained, blocked,
   restart-required, not applicable, or inconclusive.

## Configuration ownership decision

A dedicated `conf.d/99-dbtune-managed.conf` is a good ownership boundary for
self-managed VM/bare-metal PostgreSQL, but it cannot be the only execution
backend:

- The cluster must already include `conf.d` from `postgresql.conf`.
- Existing `postgresql.auto.conf` entries or command-line settings can override
  the managed file and must be detected.
- File updates must use an atomic temp-write, fsync, rename, reload, and
  sourcefile/value verification sequence.
- Rollback should restore the exact previous managed-file bytes or remove the
  file, then reload and verify value and provenance.
- Filesystem writes require a host-side privileged executor and are less
  portable and less least-privileged than parameter-scoped `ALTER SYSTEM`.
- Managed PostgreSQL platforms require API- or SQL-based adapters instead.
- Restart-context settings still require a restart; `pg_reload_conf()` alone
  cannot activate them.

Recommended abstraction: a configuration backend interface with
`alter_system`, `managed_conf_file`, and cloud-provider adapters. Prefer the
managed file on explicitly enrolled self-managed hosts; retain `ALTER SYSTEM`
as the safe default and compatibility path.

## Evidence captures

- `01-report-search.png`
- `02-runs-empty.png`
- `03-plans.png`
- `04-evidence.png`
- `05-evidence-loaded.png`
- `06-report-loaded.png`
