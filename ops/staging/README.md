# Production staging and soak runbook

Task 28 qualifies a release candidate; it does not enable production target
writes. The staging stack always sets both write switches to `false` and leaves
the confirmation empty.

## Environment boundary

Use an isolated host or VM with persistent Docker volumes, routable HTTPS, a
real alert receiver, and storage outside the host for copied backup artifacts.
Only TCP 443 should be exposed. The API, PostgreSQL, Redis, Prometheus, and
Alertmanager ports bind to loopback or the private Compose network.

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
a control-plane backup into a disposable database. During the window, also
perform and record these operator drills:

1. Stop the target Host Agent long enough to verify local evidence buffering,
   then reconnect and verify chronological replay.
2. Start a second agent with the same host identity and a different instance
   ID. Verify the critical duplicate alert and that target writes are blocked;
   then remove it and verify the resolved event.
3. Exercise a measured regression in the tuning lab and verify automatic
   byte-exact configuration rollback.
4. Deliver a test alert through the real paging route and capture its receipt.
5. Copy a backup off-host and restore it on an independent PostgreSQL instance.

## Go/no-go

`summary.json` may say `GO` only after the full qualification duration, at
least 99.5% successful readiness samples, ongoing target transaction progress,
disabled control-plane write interlocks, and all automatic drills passing.

Release remains **NO-GO** if any of the following are true:

- an unresolved critical alert, duplicate agent, stale worker lease, or failed rollback;
- restore has not been proven on an independent database;
- alert delivery has not been acknowledged;
- TLS, least-privilege credentials, or backup retention are placeholders;
- the 24-hour minimum has not elapsed;
- provider-managed targets or restart-context changes are in the launch scope.

The initial launch scope is one self-managed PostgreSQL target, reload-only
parameters, explicit approval for every candidate, and production target writes
enabled only during a staffed change window after a separate go/no-go review.
