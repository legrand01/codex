# Autonomous Postgres DBA Agent Platform

Production operators should follow the [P0 production release runbook](docs/production-release-p0.md).
For a disposable database with sustained traffic, use the [P0 tuning lab](docs/tuning-lab.md).

A web-based control plane for autonomous PostgreSQL investigation and tuning loops. The platform enables database administrators to run guarded, autonomous DBA loops that follow a structured workflow: **observe -> snapshot -> diagnose -> propose plan -> safety check -> approval gate -> dry-run -> apply -> verify -> measure -> keep/rollback -> report**.

Every action is auditable, every change is rollback-aware, and human approval is required before any write operation reaches a production database.

---

## Architecture Overview

The platform consists of five primary components:

```
+-------------------+       +--------------------+       +------------------+
|   Web Browser     |       |  Application       |       |  Data Layer      |
|                   |       |  Container         |       |                  |
|  React + TS UI    |<----->|  FastAPI Server    |<----->|  PostgreSQL      |
|  Fleet Overview   |       |  Guardrail Engine  |       |  (Platform DB)   |
|  Loop Monitoring  |       |  AI Planning       |       |                  |
|  Plan Approval    |  WS   |  DBA Loop Worker   |       |  Redis Streams   |
|  Evidence Viewer  |<----->|  Audit Logger      |       |  (Realtime/Cache)|
+-------------------+       +--------------------+       +------------------+
                                     ^
                                     |  HTTP/REST
                                     v
                            +--------------------+
                            |  PostgreSQL Fleet  |
                            |                    |
                            |  Host Agent 1      |----> Target PG Host 1
                            |  Host Agent 2      |----> Target PG Host 2
                            |  Host Agent N      |----> Target PG Host N
                            +--------------------+
```

### Component Breakdown

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Control Plane** | FastAPI (Python) | REST/WebSocket API, UI serving, orchestration |
| **Host Agent** | Python | Deployed near each PG host; collects telemetry and evidence |
| **AI Planning Module** | LLM (OpenAI-compatible) | Evidence-grounded diagnosis and plan generation |
| **Guardrail Engine** | Python | Allowlist enforcement, risk scoring, dry-run verification |
| **DBA Loop Worker** | Python (async) | Iterative observe/diagnose/plan/verify cycle orchestration |
| **Frontend** | React + TypeScript (Vite) | Real-time fleet monitoring, plan approval UI |
| **Database** | PostgreSQL 16 | Platform state, audit logs, evidence storage |
| **Message Queue** | Redis 7 (Streams) | Real-time events, worker coordination, caching |

### Key Architectural Principles

1. **Safety-First** - No write operation reaches a database without: allowlist check -> risk scoring -> human approval -> dry-run
2. **Audit Everything** - Every decision, action, and outcome is logged in an append-only audit trail
3. **Evidence-Grounded** - AI recommendations must reference collected evidence; no hallucinated metrics
4. **Rollback-Aware** - Every change has a corresponding reversal action stored at plan generation time
5. **Graceful Degradation** - Components handle disconnection, timeouts, and partial failures without data loss

---

## Local Development Setup

### Prerequisites

- **Python 3.9+** (3.11 recommended)
- **Node.js 18+** (for frontend development)
- **Docker** and **Docker Compose** (for PostgreSQL and Redis)
- **Git**

### Quick Start

1. **Clone the repository and navigate to the project:**

   ```bash
   cd autonomous-postgres-dba-agent
   ```

2. **Run the automated setup script:**

   ```bash
   ./scripts/dev-setup.sh
   ```

   This will:
   - Create a Python virtual environment
   - Install all backend dependencies
   - Install frontend dependencies (if Node.js available)
   - Start PostgreSQL and Redis via Docker
   - Launch the backend with hot-reload
   - Launch the frontend dev server
   - Confirm HTTP readiness within 60 seconds

3. **Or set up manually:**

   ```bash
   # Create and activate virtual environment
   python3 -m venv venv
   source venv/bin/activate

   # Install dependencies
   pip install -e ".[dev]"

   # Copy environment configuration
   cp .env.example .env

   # Start infrastructure
   docker compose up -d postgres redis

   # Run database migrations (auto-applied on first start)
   # Start the backend
   uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

   # In another terminal - start frontend
   cd frontend
   npm install
   npm run dev
   ```

4. **Verify the setup:**

   ```bash
   # Backend health check
   curl http://localhost:8000/health
   # Expected: {"status":"healthy","service":"autonomous-postgres-dba-agent"}

   # API documentation
   open http://localhost:8000/docs
   ```

---

## Environment Variables

All environment variables can be configured in a `.env` file at the project root. Copy `.env.example` as a starting point.

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `APP_NAME` | Application display name | `Autonomous Postgres DBA Agent Platform` | `My DBA Platform` |
| `DEBUG` | Enable debug mode (verbose logging) | `true` | `false` |
| `HOST` | Server bind address | `0.0.0.0` | `127.0.0.1` |
| `PORT` | Backend API port | `8000` | `9000` |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://postgres:postgres@localhost:5432/dba_agent` | `postgresql://user:pass@db:5432/mydb` |
| `DB_POOL_MIN_SIZE` | Minimum database connection pool size | `5` | `2` |
| `DB_POOL_MAX_SIZE` | Maximum database connection pool size | `20` | `50` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` | `redis://:password@redis:6379/1` |
| `CORS_ORIGINS` | Allowed CORS origins (JSON array) | `["http://localhost:5173","http://localhost:3000"]` | `["https://app.example.com"]` |
| `DEMO_MODE` | Enable demo mode with synthetic data | `false` | `true` |
| `RISK_THRESHOLD` | Maximum acceptable risk score (0-100) | `70` | `50` |
| `DRY_RUN_TIMEOUT_SEC` | Dry-run execution timeout in seconds | `30` | `60` |
| `APPROVAL_TIMEOUT_HOURS` | Hours before unapproved plans timeout | `24` | `48` |
| `MAX_ITERATIONS` | Maximum loop iterations per run | `10` | `5` |
| `MAX_STEPS` | Maximum steps per goal decomposition | `20` | `15` |
| `VERIFICATION_WINDOW_SEC` | Post-apply verification window (10-600s) | `60` | `120` |
| `DEGRADATION_THRESHOLD_PCT` | Metric degradation threshold for auto-rollback (%) | `10.0` | `5.0` |
| `EVIDENCE_CLEANUP_ENABLED` | Run scheduled tenant evidence maintenance | `true` | `false` |
| `EVIDENCE_RAW_RETENTION_DAYS` | Ordinary raw evidence retention | `30` | `14` |
| `EVIDENCE_REFERENCED_RETENTION_DAYS` | Raw retention for durable references | `90` | `120` |
| `EVIDENCE_ROLLUP_RETENTION_DAYS` | Compact aggregate history retention | `365` | `730` |
| `EVIDENCE_CLEANUP_INTERVAL_SECONDS` | Scheduled maintenance interval | `3600` | `21600` |
| `EVIDENCE_CLEANUP_BATCH_SIZE` | Maximum raw rows per cleanup/backfill batch | `1000` | `500` |
| `EVIDENCE_CLEANUP_MAX_BATCHES` | Maximum deletion batches per maintenance run | `20` | `10` |
| `PG_SETTINGS_INTERVAL_SEC` | pg_settings collection interval (10-3600s) | `60` | `120` |
| `PG_STATS_INTERVAL_SEC` | pg_stat collection interval (5-600s) | `30` | `15` |
| `LOCKS_REPLICATION_INTERVAL_SEC` | Locks/replication collection interval (5-300s) | `15` | `30` |
| `OS_METRICS_INTERVAL_SEC` | OS metrics collection interval (5-300s) | `15` | `10` |
| `MANAGED_FILE_ACCESS` | Explicitly enroll Host Agent file ownership | `false` | `true` |
| `MANAGED_CONF_PATH` | Exact enrolled PostgreSQL include path | empty | `/var/lib/postgresql/data/conf.d/postgres_tune.conf` |
| `COMMAND_POLL_INTERVAL` | Host Agent command polling interval (seconds) | `2` | `1` |

---

## Running Tests

### Full Test Suite

```bash
./scripts/run-tests.sh
```

This executes all test categories:
- **Guardrail Enforcement** - Allowlist validation, risk scoring, safety workflow ordering
- **Loop Execution** - DBA loop worker, run management, post-apply verification
- **Evidence Lifecycle** - bounded raw retention, reference protection, atomic daily rollups, and cleanup history
- **Evidence Collection** - Host agent, evidence buffering, evidence API
- **Plan Generation** - AI planning, plan approval, report generation

### Individual Test Categories

```bash
# Run all tests
pytest tests/ -v

# Guardrail tests only
pytest tests/test_guardrail_allowlist.py tests/test_guardrail_safety.py tests/test_risk_scoring.py -v

# Loop execution tests
pytest tests/test_loop_worker.py tests/test_runs_api.py tests/test_verification.py -v

# Evidence tests
pytest tests/test_evidence_api.py tests/test_evidence_buffer.py tests/test_host_agent.py -v

# Plan generation tests
pytest tests/test_plans_api.py tests/test_ai_planning.py tests/test_reports.py -v

# Property-based tests only
pytest tests/ -m property -v
```

### Test Configuration

Property-based tests use Hypothesis with the following settings:
- `max_examples=100` for thorough coverage in CI
- Configurable via `pytest.ini` or `pyproject.toml`

---

## Deployment

### Docker Compose (Recommended)

```bash
# Deploy all services
./scripts/deploy.sh

# Or manually:
docker compose up -d --build

# Check status
docker compose ps

# View logs
docker compose logs -f app
```

### Custom Host/Port

```bash
# Deploy on custom port
PORT=9000 FRONTEND_PORT=3000 ./scripts/deploy.sh
```

### Health Verification

After deployment, the platform should return a valid HTTP response within 30 seconds:

```bash
curl http://localhost:8000/health
```

---

## Demo Walkthrough

This walkthrough exercises the complete plan-generation-to-execution workflow using Demo Mode.

### Step 1: Enable Demo Mode

```bash
# Start with demo mode enabled
DEMO_MODE=true docker compose up -d --build

# Or via API after startup:
curl -X POST http://localhost:8000/api/v1/demo/activate
```

The platform seeds realistic data: 3+ PostgreSQL hosts with varying health states, synthetic evidence, and pre-configured guardrail allowlists.

### Step 2: View Fleet Overview

```bash
# List all registered hosts
curl http://localhost:8000/api/v1/fleet/
```

You should see hosts with different statuses: connected/degraded/disconnected, healthy/unhealthy.

### Step 3: Start an Autonomous DBA Loop

```bash
# Submit a tuning goal
curl -X POST http://localhost:8000/api/v1/runs/ \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Investigate and optimize slow query performance on the primary host",
    "host_id": "<host-id-from-step-2>"
  }'
```

Note the `run_id` from the response.

### Step 4: Monitor Loop Progress

```bash
# Check run status
curl http://localhost:8000/api/v1/runs/<run-id>

# List active runs
curl http://localhost:8000/api/v1/runs/
```

Watch as the loop transitions through: observe -> diagnose -> propose_plan -> safety_check -> approval_gate.

### Step 5: View Collected Evidence

```bash
# List evidence for the run
curl http://localhost:8000/api/v1/evidence/<run-id>

# Get a specific snapshot
curl http://localhost:8000/api/v1/evidence/snapshot/<snapshot-id>
```

Evidence is categorized by type: configuration, performance, locks, replication, WAL/checkpoint, OS metrics.

### Step 6: Review and Approve a Plan

```bash
# List pending plans
curl http://localhost:8000/api/v1/plans/

# View plan details (shows proposed changes, risk score, evidence refs, rollback instructions)
curl http://localhost:8000/api/v1/plans/<plan-id>

# Approve the plan (triggers dry-run then apply)
curl -X POST http://localhost:8000/api/v1/plans/<plan-id>/approve \
  -H "Content-Type: application/json" \
  -d '{"dba_id": "demo-admin"}'
```

### Step 7: Observe Post-Apply Verification

After approval, the system:
1. Executes a dry-run to validate SQL
2. Applies the change
3. Collects verification evidence
4. Compares pre/post metrics
5. Keeps the change or initiates automatic rollback

### Step 8: View the Final Report

```bash
# Get the DBA report (generated after loop completes)
curl http://localhost:8000/api/v1/reports/<run-id>
```

The report contains: original goal, evidence summaries with confidence scores, plans proposed, approval decisions, applied changes, verification results, and final outcome.

### Step 9: Review the Audit Trail

```bash
# View audit log for the run
curl http://localhost:8000/api/v1/audit/<run-id>
```

Every decision, approval, rejection, and system action is recorded with timestamps and actor identity.

Operational events are also available as stable codes for alerting and filtering. The UI exposes
them under **Events**, while the immutable audit log remains the compliance record.

### Host Agent identity and configuration history

Each agent installation must use a unique, persistent `AGENT_INSTANCE_ID` and private state
volume. If two active instances report the same host identity, target writes are blocked until
one lease expires. Fleet diagnostics show the active lease, independent capabilities, and
version/backend-specific least-privilege setup instructions.

Configuration versions are recorded for every backend. Operators can compare versions and
download a redacted `.conf` export. Reapplying a rolled-back or superseded verified version
creates a fresh workload baseline and a new pending-approval plan; it never bypasses dry-run,
verification, measurement, or rollback guardrails.

### Step 10: Test Rollback (Optional)

```bash
# If a plan was applied, initiate rollback
curl -X POST http://localhost:8000/api/v1/rollback/<plan-id>

# Check rollback status
curl http://localhost:8000/api/v1/rollback/<plan-id>/status
```

---

## Project Structure

```
autonomous-postgres-dba-agent/
├── backend/                  # FastAPI application
│   ├── api/                  # API route handlers
│   │   ├── audit.py          # Audit log endpoints
│   │   ├── evidence.py       # Evidence viewer endpoints
│   │   ├── fleet.py          # Fleet management endpoints
│   │   ├── plans.py          # Plan approval queue endpoints
│   │   ├── reports.py        # DBA report endpoints
│   │   ├── rollback.py       # Rollback control endpoints
│   │   ├── runs.py           # Loop run management endpoints
│   │   └── ws_fleet.py       # WebSocket for real-time fleet updates
│   ├── db/                   # Database layer
│   │   ├── init_db.py        # Database initialization and migrations
│   │   ├── migrations/       # SQL migration files
│   │   ├── pool.py           # Connection pool management
│   │   ├── redis_manager.py  # Redis connection management
│   │   └── repository.py     # Base repository with CRUD patterns
│   ├── models/               # Pydantic data models and enums
│   ├── services/             # Business logic services
│   │   ├── ai_planning.py    # AI diagnosis and plan generation
│   │   ├── audit_logger.py   # Append-only audit logging
│   │   ├── fleet_service.py  # Fleet and heartbeat management
│   │   ├── guardrail_engine.py  # Safety checks and enforcement
│   │   ├── loop_worker.py    # DBA loop execution engine
│   │   ├── redaction.py      # Secret redaction for audit entries
│   │   ├── rollback_service.py  # Rollback execution
│   │   └── verification.py   # Post-apply metric verification
│   ├── config.py             # Application settings
│   ├── dependencies.py       # FastAPI dependency injection
│   └── main.py               # Application entry point
├── frontend/                 # React + TypeScript frontend
│   └── src/
│       ├── components/       # Shared UI components
│       ├── pages/            # Page components
│       └── api/              # API client
├── host_agent/               # Host agent for evidence collection
│   ├── agent.py              # Main agent loop
│   ├── buffer.py             # Local evidence buffering
│   ├── collectors/           # Evidence collection modules
│   └── config.py             # Agent configuration
├── docker/                   # Docker configuration
│   ├── Dockerfile.backend    # Backend production image
│   ├── Dockerfile.frontend   # Frontend production image
│   └── nginx.conf            # Nginx reverse proxy config
├── scripts/                  # Automation scripts
│   ├── dev-setup.sh          # Local development setup
│   ├── run-tests.sh          # Automated test runner
│   └── deploy.sh             # Deployment script
├── tests/                    # Test suite
├── docker-compose.yml        # Multi-service orchestration
├── pyproject.toml            # Python project configuration
├── .env.example              # Environment variable template
└── README.md                 # This file
```

---

## API Reference

Full interactive API documentation is available at `http://localhost:8000/docs` (Swagger UI) or `http://localhost:8000/redoc` (ReDoc) when the server is running.

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/fleet/` | List all hosts |
| POST | `/api/v1/fleet/` | Register a host |
| GET | `/api/v1/fleet/{id}/diagnostics` | Agent leases and independent capabilities |
| GET | `/api/v1/fleet/{id}/setup` | Least-privilege setup guide |
| GET | `/api/v1/runs/` | List active runs |
| POST | `/api/v1/runs/` | Start a new DBA loop |
| POST | `/api/v1/runs/{id}/halt` | Halt an active run |
| GET | `/api/v1/evidence/{run_id}` | List evidence for a run |
| GET | `/api/v1/plans/` | List pending plans |
| POST | `/api/v1/plans/{id}/approve` | Approve a plan |
| POST | `/api/v1/plans/{id}/reject` | Reject a plan |
| POST | `/api/v1/rollback/{plan_id}` | Initiate rollback |
| GET | `/api/v1/audit/{run_id}` | View audit log |
| GET | `/api/v1/events/` | Filter coded operational events |
| GET | `/api/v1/configurations/` | Configuration history by host/database |
| GET | `/api/v1/configurations/compare` | Compare two configuration versions |
| GET | `/api/v1/configurations/{id}/download` | Download a redacted configuration |
| POST | `/api/v1/configurations/{id}/reapply` | Request guarded reapply with fresh baseline |
| GET | `/api/v1/reports/{run_id}` | Get DBA report |

---

## License

MIT
