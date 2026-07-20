# Technical Design Document

## Overview

The Autonomous Postgres DBA Agent Platform is a web-based system that enables database administrators to manage PostgreSQL fleets through autonomous investigation and tuning loops. The platform follows a structured workflow (observe → snapshot → diagnose → propose plan → safety check → approval gate → dry-run → apply → verify → measure → keep/rollback → report) with comprehensive safety guardrails and audit logging.

The system consists of five primary components:
1. **Control Plane** — A web application providing fleet overview, loop monitoring, evidence viewing, plan approval, and rollback controls
2. **Host Agent** — A lightweight service deployed near each PostgreSQL host collecting telemetry and evidence
3. **AI Planning Module** — An analytical engine consuming evidence to produce diagnostic recommendations with confidence scores
4. **Guardrail Engine** — A safety subsystem enforcing allowlists, risk scoring, dry-run verification, and approval gates
5. **DBA Loop Worker** — An orchestrator executing iterative observe/diagnose/plan/verify cycles

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Backend Framework | Python (FastAPI) | Async-native, excellent PostgreSQL library ecosystem (asyncpg, psycopg), strong AI/ML integration |
| Frontend Framework | React + TypeScript | Strong typing, component ecosystem, real-time update patterns via WebSockets |
| Database | PostgreSQL | Self-referential choice — platform manages Postgres, uses Postgres for its own state |
| Message Queue | Redis Streams | Lightweight, supports consumer groups for worker coordination, low-latency pub/sub for real-time UI |
| AI Integration | LLM via OpenAI-compatible API | Structured output for plan generation, evidence grounding through prompt engineering |
| Containerization | Docker + docker-compose | Single-command local dev, consistent environments, production-ready |
| Testing | pytest + Hypothesis (property-based) | Comprehensive testing with property-based validation of guardrail logic |
| Product Information Architecture | Session-centric tuning workspace | Runs, Plans, Evidence, Configuration, Activity, Rollback, and Report share one persistent run context instead of separate UUID-driven queues |
| Optimization Method | Baseline-and-candidate search | Candidate configurations are measured against a stable Workload_Fingerprint, AQR, TPS, or composite objective; no setting is called beneficial before verification |
| Configuration Apply | Pluggable Configuration_Backend | Parameter-scoped ALTER SYSTEM remains the portable least-privilege default; an atomic DBTune-owned conf.d file is preferred on explicitly enrolled self-managed hosts; provider APIs handle managed services |

### DBTune Baseline Research

The July 12, 2026 baseline was verified against the authenticated DBTune product
and its public documentation. Accepted live captures are stored in
`docs/postgres-dba-demo-assets/dbtune-live-2026-07-12/`.

The baseline establishes these product behaviors:

- a per-database workspace with Dashboard, Tuning, Fingerprints,
  Configuration history, Event logs, and Agent tabs;
- a tuning-session selector and performance charts with time windows;
- recommended or custom Workload_Fingerprints based on query AQR, calls, total
  duration, runtime coverage, and last-seen time;
- workload-fingerprint and system-wide tuning modes;
- optional human-in-the-loop approval, reload-only and restart-enabled modes,
  explicit parameter selection, and separate AQR/TPS/fingerprint guardrails;
- configuration history with compare, download, and guarded apply actions;
- coded, filterable event logs and agent capability/setup diagnostics.

DBTune's Community PostgreSQL integration uses ALTER SYSTEM. This design adds a
managed-file backend as a deliberate self-managed-host option, not as a claim
that the reference product edits conf.d or that file access is portable to
managed PostgreSQL services.

Primary references:

- https://docs.dbtune.com/overview/
- https://docs.dbtune.com/server-parameter-tuning/
- https://docs.dbtune.com/tuning-targets/
- https://docs.dbtune.com/tuning-modes/
- https://docs.dbtune.com/Human-in-the-loop/
- https://docs.dbtune.com/postgresql/
- https://www.postgresql.org/docs/current/sql-altersystem.html
- https://www.postgresql.org/docs/current/config-setting.html
- https://www.postgresql.org/docs/current/view-pg-file-settings.html

## Architecture

### System Architecture Diagram

```mermaid
graph TB
    subgraph "Web Browser"
        UI[React Control Plane UI]
    end

    subgraph "Application Container"
        API[FastAPI Application Server]
        WS[WebSocket Handler]
        GE[Guardrail Engine]
        AIP[AI Planning Module]
        LW[DBA Loop Worker]
        AL[Audit Logger]
        OE[Candidate Optimizer]
        CB[Configuration Backend Router]
    end

    subgraph "Data Layer"
        PG[(PostgreSQL - Platform DB)]
        RD[(Redis - Streams & Cache)]
    end

    subgraph "PostgreSQL Fleet"
        HA1[Host Agent 1]
        HA2[Host Agent 2]
        HA3[Host Agent N]
        PG1[(Target PG Host 1)]
        PG2[(Target PG Host 2)]
        PG3[(Target PG Host N)]
    end

    UI --> API
    UI <--> WS
    API --> GE
    API --> AIP
    API --> LW
    API --> AL
    LW --> GE
    LW --> AIP
    LW --> AL
    LW --> OE
    OE --> GE
    GE --> CB
    GE --> AL
    AIP --> AL
    
    API --> PG
    API --> RD
    LW --> RD
    
    HA1 --> API
    HA2 --> API
    HA3 --> API
    HA1 --> PG1
    HA2 --> PG2
    HA3 --> PG3
    CB --> HA1
    CB --> HA2
    CB --> HA3
```

### Component Communication

```mermaid
sequenceDiagram
    participant DBA as DBA (Browser)
    participant CP as Control Plane API
    participant LW as DBA Loop Worker
    participant HA as Host Agent
    participant AI as AI Planning Module
    participant GE as Guardrail Engine
    participant AL as Audit Log
    participant DB as Platform DB

    DBA->>CP: Submit goal
    CP->>LW: Start loop run
    LW->>AL: Log run start
    
    loop Observe/Diagnose/Plan/Verify
        LW->>HA: Request evidence collection
        HA->>DB: Store evidence snapshots
        HA-->>LW: Evidence collected
        LW->>AI: Diagnose with evidence
        AI-->>LW: Recommendations + confidence
        LW->>AI: Generate plan
        AI-->>LW: Plan with rollback instructions
        LW->>GE: Submit plan for safety check
        GE->>AL: Log risk assessment
        GE-->>LW: Risk score + allowlist result
        LW->>CP: Queue plan for approval
        CP->>DBA: Notify plan ready
        DBA->>CP: Approve/Reject plan
        CP->>AL: Log approval decision
        CP->>GE: Execute dry-run
        GE-->>CP: Dry-run result
        CP->>HA: Apply changes
        LW->>HA: Collect verification evidence
        LW->>LW: Compare pre/post metrics
    end
    
    LW->>LW: Generate DBA Report
    LW->>AL: Log run completion
    LW->>DBA: Report available
```

### Key Architectural Principles

1. **Safety-First**: No write operation reaches a database without passing through allowlist check → risk scoring → human approval → dry-run
2. **Audit Everything**: Every decision, action, and outcome is logged in an append-only audit trail
3. **Evidence-Grounded**: AI recommendations must reference collected evidence; no hallucinated metrics
4. **Rollback-Aware**: Every change has a corresponding reversal action stored at plan generation time
5. **Graceful Degradation**: Components handle disconnection, timeouts, and partial failures without data loss

## Components and Interfaces

### 1. Control Plane API (FastAPI)

**Responsibilities**: HTTP/WebSocket API, authentication, routing, UI serving

```python
# API Route Groups
/api/v1/fleet/          # Fleet overview and host management
/api/v1/runs/           # Loop run management and monitoring
/api/v1/evidence/       # Evidence viewing and querying
/api/v1/plans/          # Plan review, approval, rejection
/api/v1/rollback/       # Rollback initiation and monitoring
/api/v1/audit/          # Audit log querying
/api/v1/reports/        # DBA report retrieval and search
/api/v1/sessions/       # Persistent tuning-session history and workspace summaries
/api/v1/fingerprints/   # Recommended/custom workload fingerprints
/api/v1/configurations/ # Configuration versions, compare, download, guarded apply
/api/v1/events/         # Filterable operational event history
/api/v1/agents/         # Agent capabilities, duplicate detection, setup diagnostics
/api/v1/guardrails/     # Guardrail configuration (allowlists, thresholds)
/api/v1/demo/           # Demo mode management
/ws/runs/{run_id}       # WebSocket for real-time run updates
/ws/fleet               # WebSocket for fleet status updates
/health                 # Health check endpoint
```

**Key Interfaces**:

```python
class FleetAPI:
    async def list_hosts() -> List[HostSummary]
    async def get_host(host_id: str) -> HostDetail
    async def register_host(config: HostRegistration) -> Host

class RunsAPI:
    async def start_run(goal: RunGoal) -> RunResponse
    async def halt_run(run_id: str) -> HaltResponse
    async def get_run_status(run_id: str) -> RunStatus
    async def list_active_runs() -> List[RunSummary]
    async def list_runs(filters: RunFilters, page: int, page_size: int) -> PaginatedRuns
    async def get_workspace(run_id: str) -> TuningSessionWorkspace

class TuningSessionsAPI:
    async def start_session(request: StartTuningSession) -> TuningSession
    async def list_sessions(filters: SessionFilters) -> PaginatedSessions
    async def get_session(run_id: str) -> TuningSessionWorkspace

class FingerprintsAPI:
    async def recommend(host_id: str, window: TimeRange) -> FingerprintRecommendation
    async def create(request: CreateFingerprint) -> WorkloadFingerprint
    async def list(host_id: str) -> List[WorkloadFingerprint]

class ConfigurationHistoryAPI:
    async def list_versions(host_id: str) -> List[ConfigurationVersion]
    async def compare(left_id: str, right_id: str) -> ConfigurationDiff
    async def request_apply(version_id: str) -> Plan

class EventsAPI:
    async def list_events(filters: EventFilters) -> PaginatedEvents

class AgentsAPI:
    async def get_capabilities(host_id: str) -> AgentCapabilities
    async def get_setup_instructions(host_id: str, mode: str, backend: str) -> SetupGuide

class PlansAPI:
    async def list_pending_plans(page: int, page_size: int) -> PaginatedPlans
    async def get_plan(plan_id: str) -> PlanDetail
    async def approve_plan(plan_id: str, dba_id: str) -> ApprovalResult
    async def reject_plan(plan_id: str, dba_id: str, reason: str) -> RejectionResult

class RollbackAPI:
    async def initiate_rollback(plan_id: str) -> RollbackResponse
    async def get_rollback_status(plan_id: str) -> RollbackStatus

class EvidenceAPI:
    async def list_evidence(run_id: str, category: Optional[str]) -> List[EvidenceSummary]
    async def get_evidence_snapshot(snapshot_id: str) -> EvidenceSnapshot

class AuditAPI:
    async def get_audit_log(run_id: str) -> List[AuditEntry]

class ReportsAPI:
    async def get_report(run_id: str) -> DBAReport
    async def search_reports(query: ReportSearchQuery) -> List[ReportSummary]
```

### 2. Host Agent

**Responsibilities**: Evidence collection, local buffering, heartbeat, change application

```python
class HostAgent:
    async def collect_pg_settings() -> PgSettingsSnapshot
    async def collect_pg_stats() -> PgStatsSnapshot
    async def collect_locks() -> LockSnapshot
    async def collect_replication() -> ReplicationSnapshot
    async def collect_wal_checkpoint() -> WALSnapshot
    async def collect_os_metrics() -> OSMetricsSnapshot
    async def apply_changes(plan: ApprovedPlan) -> ApplyResult
    async def execute_dry_run(plan: Plan) -> DryRunResult
    async def report_heartbeat() -> None
    async def buffer_evidence(snapshot: EvidenceSnapshot) -> None
    async def flush_buffer() -> None
    async def report_capabilities() -> AgentCapabilities
    async def inspect_configuration_precedence(settings: List[str]) -> PrecedenceReport
    async def validate_managed_configuration(rendered: bytes) -> FileValidationResult
    async def atomic_write_managed_configuration(rendered: bytes) -> ManagedFileVersion
    async def restore_managed_configuration(version: ManagedFileVersion) -> ApplyResult
```

**Communication Protocol**: HTTP POST to Control Plane API with retry and local buffering (max 512 MB).

### 3. AI Planning Module

**Responsibilities**: Evidence analysis, recommendation generation, plan creation

```python
class AIPlanningModule:
    async def diagnose(evidence: List[EvidenceSnapshot], goal: str) -> DiagnosisResult
    async def generate_plan(
        diagnosis: DiagnosisResult,
        evidence: List[EvidenceSnapshot],
        current_settings: PgSettingsSnapshot,
        rejection_feedback: Optional[str] = None
    ) -> Plan
    
    def check_evidence_quality(evidence: List[EvidenceSnapshot]) -> EvidenceQualityReport
    def calculate_confidence(recommendation: Recommendation, evidence: List[EvidenceSnapshot]) -> float
```

**Constraints**:
- Must only reference evidence from current loop run
- Must never fabricate metrics not in or derivable from evidence
- Must include rollback instructions for every proposed change
- Must mark recommendations as inconclusive when evidence quality is insufficient

### 4. Guardrail Engine

**Responsibilities**: Allowlist enforcement, risk scoring, dry-run, approval gate, workflow ordering

```python
class GuardrailEngine:
    async def check_allowlist(plan: Plan, host_id: str) -> AllowlistResult
    def calculate_risk_score(plan: Plan, host: Host) -> RiskScore
    async def execute_dry_run(plan: Plan, host_id: str, timeout: int = 30) -> DryRunResult
    async def validate_rollback_plan(plan: Plan, pre_snapshot: PgSettingsSnapshot) -> RollbackValidation
    async def enforce_approval_gate(plan_id: str) -> ApprovalGateResult
    def get_allowlist(host_id: str) -> List[AllowlistEntry]
    def update_allowlist(host_id: str, entries: List[AllowlistEntry]) -> None
    
    # Safety workflow: risk_score → allowlist → approval → dry_run → apply
    async def full_safety_check(plan: Plan, host_id: str) -> SafetyCheckResult
```

**Risk Score Calculation**:
```
risk_score = min(100, Σ(setting_risk_i))
setting_risk_i = deviation_weight(%) * host_role_multiplier * setting_criticality
host_role_multiplier: primary=1.5, replica=1.0
deviation_weight: |proposed - current| / current * 100
```

### 5. DBA Loop Worker

**Responsibilities**: Goal decomposition, iterative execution, report generation

```python
class DBALoopWorker:
    async def start_run(goal: str, config: LoopConfig) -> RunResult
    async def halt_run(run_id: str) -> None
    async def decompose_goal(goal: str) -> List[WorkflowStep]
    
    # Workflow steps
    async def observe(run_id: str, host_id: str) -> EvidenceSet
    async def diagnose(evidence: EvidenceSet) -> DiagnosisResult
    async def propose_plan(diagnosis: DiagnosisResult) -> Plan
    async def verify(plan: AppliedPlan, pre_evidence: EvidenceSet) -> VerificationResult
    async def generate_report(run_id: str) -> DBAReport
```

**Loop Configuration**:
- Max iterations: configurable, default 10
- Max steps per iteration: configurable, default 20
- Approval timeout: configurable, default 24 hours
- Verification window: configurable, default 60 seconds (range: 10-600s)
- Degradation threshold: configurable, default 10%

### 6. Audit Logger

**Responsibilities**: Append-only logging, secrets redaction, structured entries

```python
class AuditLogger:
    async def log(entry: AuditEntry) -> None
    def redact_secrets(content: str) -> str
    async def query(run_id: Optional[str], time_range: Optional[TimeRange]) -> List[AuditEntry]
    
    # Secret patterns to redact
    SECRET_PATTERNS = [
        r'password\s*=\s*\S+',
        r'postgresql://[^@]+@',
        r'[A-Za-z0-9+/]{40,}={0,2}',  # Base64 tokens
        r'(sk|pk|api)[-_][A-Za-z0-9]{20,}',  # API keys
    ]
```

### 7. Tuning Session Workspace

**Responsibilities**: Persistent session navigation, run history, unified run
context, start-tuning flow, and safe action discovery.

```text
Primary navigation
├── Fleet
├── Tuning
│   ├── Start tuning
│   ├── Session history (all statuses)
│   └── /tuning/{run_id}
│       ├── Overview
│       ├── Configuration
│       ├── Workload
│       ├── Evidence
│       ├── Activity
│       └── Report
├── Reports
└── Administration
    ├── Guardrails
    ├── Agent
    └── Events
```

The workspace owns the selected `run_id`; child views receive it through the
route and never ask the DBA to paste it. A persistent header exposes host,
database, objective, mode, status, workflow step, baseline, best score,
current candidate, start/completion times, and eligible actions. Completed and
failed sessions remain in history. Active state is a filter, not a retention
policy.

### 8. Workload Fingerprint and Candidate Optimizer

**Responsibilities**: Define a stable objective, capture a baseline, generate
bounded candidates, measure them comparably, and retain only verified gains.

Fingerprint recommendations rank normalized statements with both average query
runtime and call count, then select enough members to cover the dominant visible
runtime. Readiness fails closed when the collector is truncated, coverage is
below 70%, membership stability is below 60%, or runtime variance exceeds 50%.
Query text is persisted only when explicitly enabled. Every saved fingerprint
is an immutable membership version; refreshing a recommendation creates a new
version so prior tuning sessions remain repeatable.

```python
class CandidateOptimizer:
    async def capture_baseline(session: TuningSession) -> BaselineMeasurement
    async def propose_candidate(
        session: TuningSession,
        history: List[CandidateMeasurement],
    ) -> TuningCandidate
    async def measure_candidate(candidate_id: UUID) -> CandidateMeasurement
    async def decide(candidate_id: UUID) -> CandidateDecision
    async def restore_best_or_baseline(session_id: UUID) -> ApplyResult
```

Candidate sequence:

```mermaid
sequenceDiagram
    participant DBA
    participant CP as Control Plane
    participant OP as Candidate Optimizer
    participant GE as Guardrails
    participant CB as Configuration Backend
    participant HA as Host Agent

    DBA->>CP: Start tuning(host, objective, mode, parameters)
    CP->>HA: Capture workload and safety baseline
    HA-->>OP: Stable baseline + coverage/noise report
    loop Until budget, convergence, or guardrail stop
        OP->>GE: Proposed bounded candidate
        GE-->>DBA: Approval when policy requires
        GE->>CB: Apply candidate
        CB-->>OP: Verified value and provenance
        OP->>HA: Warm up and measure same objective
        HA-->>OP: Candidate score + safety metrics
        OP->>OP: Compare baseline and best-so-far
        alt Candidate beneficial and safe
            OP->>CP: Record new best
        else Regression, noise, or coverage loss
            OP->>CB: Restore best or baseline
        end
    end
    OP->>CP: Final parameter dispositions and report
```

The initial optimizer may use deterministic bounded search. Bayesian or
reinforcement-learning strategies can be introduced later behind the same
interface. The correctness boundary is the repeatable measurement protocol,
not the candidate-generation algorithm.

The P0 implementation uses domain version `p0-bounded-v1`. Each supported
setting has a typed `multipliers` or `absolute` domain with minimum and maximum
bounds. Candidate generation intersects that domain with the enrolled host's
guardrail allowlist and its maximum-deviation limit. The ordering is
deterministic, the immutable baseline value is the expansion anchor, and values
already attempted by the session are never proposed again.

Each candidate is durably stored in `tuning_candidates` with its session,
plan, iteration, domain version, parameter values, exact pre-change snapshot,
baseline and best-so-far scores, objective contract, measurement windows,
coverage, variance, safety metrics and deltas, evidence references, decision,
and reason. The full comparable measurement payload is stored before the
decision. A restarted worker therefore resumes the same measurement or decision
without silently recapturing a different evidence window.

A candidate is kept only when it beats both the immutable baseline and the
best-so-far by the configured minimum improvement and all comparability and
safety checks pass. Incomplete windows, changed fingerprint membership,
coverage loss, excessive variance, deadlocks, waiting locks, replication lag,
resource regression, or transaction-rate regression make the result
inconclusive or rolled back. Every non-kept candidate restores its exact
pre-change snapshot before the next proposal, which is the current verified
best state (or the baseline when no candidate has been kept).

Before candidate generation, a root-cause gate classifies configuration,
query-plan/index, lock, vacuum/bloat, storage/CPU, connection-pressure, and
insufficient-evidence signals. Configuration search proceeds only when a
configuration lever is plausible. Query and index findings are presented as a
separate advisory track and remain non-executable in the current P0 boundary.
The first baseline for a session is immutable. Its objective formula, direction,
units, fingerprint membership, warm-up and requested/observed windows, workload
coverage, variance, safety metrics, evidence endpoints, and root-cause result are
stored together. At least 80% of the requested window must be observed, and the
selected fingerprint must remain measurement-ready; otherwise the session pauses
without reading or writing target configuration. Non-configuration diagnoses
complete as `advisory_only`, persist recommendations with `executable = FALSE`,
and create no tuning plan. Monotonic PostgreSQL statistics are converted to
first/last counter deltas inside the requested trailing window, so historical
activity before the baseline cannot bias later candidate comparisons.

### 9. Configuration Backend Router

```python
class ConfigurationBackend(Protocol):
    async def preflight(self, host_id: UUID, settings: List[str]) -> BackendPreflight
    async def snapshot(self, host_id: UUID, settings: List[str]) -> ConfigurationSnapshot
    async def apply(self, host_id: UUID, changes: List[SettingChange]) -> ApplyResult
    async def rollback(self, host_id: UUID, snapshot: ConfigurationSnapshot) -> ApplyResult

class AlterSystemBackend(ConfigurationBackend): ...
class ManagedConfFileBackend(ConfigurationBackend): ...
class ProviderConfigurationBackend(ConfigurationBackend): ...
```

#### Backend selection

| Target | Preferred backend | Reason |
|---|---|---|
| Self-managed VM/bare metal with enrolled file access | `managed_conf_file` | Clear DBTune ownership and byte-exact version/rollback |
| Self-managed PostgreSQL without file access | `alter_system` | Portable SQL path with PostgreSQL 15+ parameter-scoped privileges |
| Managed cloud PostgreSQL | provider adapter | Provider owns parameter groups/flags and filesystem is unavailable |

#### Managed file apply protocol

1. Verify `config_file`, include/include_dir location and ordering, same-device
   atomic rename support, ownership, permissions, and available disk space.
2. Query `pg_settings` and `pg_file_settings` for source, sourcefile, context,
   pending_restart, duplicate definitions, and parse errors.
3. Fail closed if command-line options, `postgresql.auto.conf`, later include
   files, database/user settings, or provider settings override a managed key.
4. Capture the exact previous bytes, mode, owner, checksum, and effective
   values as the rollback snapshot.
5. Render only allowlisted settings to `conf.d/postgres_tune.conf` through a
   same-directory temporary file, fsync the file, atomically rename, and fsync
   the directory. Validate the final path with `pg_file_settings` before reload.
6. Call `pg_reload_conf()` for reload-context settings and verify effective
   value, `source = 'configuration file'`, and the expected `sourcefile`.
7. On any failure, atomically restore the previous file bytes or absence,
   reload, verify value and provenance, and emit a coded event.
8. Stage postmaster-context settings as pending restart; verify them only after
   a controlled restart.

`postgresql.auto.conf` is loaded after `postgresql.conf` and its includes, so a
DBTune-managed conf.d file cannot safely control a parameter that still has an
ALTER SYSTEM entry. The backend preflight therefore rejects such conflicts; it
does not silently reset configuration owned by another operator.

The control plane never reaches into the target filesystem. It persists an
authenticated command in `agent_commands`; the Host Agent claims it over its
outbound HTTPS channel, performs the local operation, and stores the result
before the worker continues. `configuration_versions` retains exact previous
bytes internally for recovery, while APIs and reports remove `bytes_b64`.
Interrupted workers reconcile an `applying` version from the durable command
result, so a successful file write is not replayed and a partial state retains
byte-exact rollback provenance.

Managed PostgreSQL services use an explicitly registered provider adapter with
preflight, stage, poll, restart request, verify, and rollback operations. If no
adapter is registered for the host platform, the provider backend fails closed;
it never emulates local file access.

### 10. Supported Parameter Catalog

Reload-only catalog:

```text
work_mem                         random_page_cost
seq_page_cost                    checkpoint_completion_target
effective_io_concurrency         max_parallel_workers_per_gather
max_parallel_workers             max_wal_size
min_wal_size                     bgwriter_lru_maxpages
bgwriter_delay                   effective_cache_size
maintenance_work_mem             default_statistics_target
max_parallel_maintenance_workers
```

Restart-enabled additions:

```text
shared_buffers   max_worker_processes   wal_buffers   huge_pages
```

The catalog is versioned by PostgreSQL major version and target platform. Every
final report includes all catalog entries with one disposition: changed and
verified, retained, blocked, restart required, unsupported, not applicable, or
inconclusive.

The control plane materializes catalog versions for PostgreSQL 15 through 18
and each supported platform family. A tuning session resolves one immutable
catalog version from the agent-reported major version and enrolled platform.
Reload-only sessions materialize the 15 online entries; restart-enabled
sessions materialize those entries plus the four postmaster entries.

`run_parameter_dispositions` is the durable session result set. It contains
exactly one row per catalog entry in the selected mode and records selection,
allowlist state, target support, current value, unit, `pg_settings` source,
source file or provider, PostgreSQL context, pending-restart state, immutable
baseline, best verified value, pending candidate, final disposition, and
reason. Active sessions may leave the final disposition empty only while an
eligible selected setting is still being evaluated. Terminal sessions reconcile
every row to exactly one Requirement 18 disposition before report generation.
The Configuration tab and final report both consume this same durable result
set; neither infers success from plan status.

## Data Models

### Core Database Schema

```sql
-- Hosts and Fleet Management
CREATE TABLE hosts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname VARCHAR(255) NOT NULL UNIQUE,
    pg_version VARCHAR(50),
    server_role VARCHAR(20) CHECK (server_role IN ('primary', 'replica')),
    health_status VARCHAR(20) DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'unhealthy', 'unknown')),
    connection_status VARCHAR(20) DEFAULT 'disconnected' CHECK (connection_status IN ('connected', 'degraded', 'disconnected')),
    last_heartbeat TIMESTAMPTZ,
    restart_required_enabled BOOLEAN DEFAULT FALSE,
    configuration_backend VARCHAR(30) DEFAULT 'alter_system' CHECK (configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')),
    managed_conf_path TEXT,
    managed_conf_enrolled BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Loop Runs
CREATE TABLE loop_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    goal TEXT NOT NULL,
    database_name VARCHAR(255),
    tuning_target VARCHAR(30) CHECK (tuning_target IN ('fingerprint', 'aqr', 'tps', 'composite')),
    tuning_mode VARCHAR(30) DEFAULT 'reload_only' CHECK (tuning_mode IN ('reload_only', 'restart_enabled')),
    fingerprint_id UUID,
    configuration_backend VARCHAR(30),
    baseline_measurement JSONB,
    best_measurement JSONB,
    status VARCHAR(30) DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'manually_halted', 'unresponsive', 'timed_out')),
    current_step VARCHAR(30) CHECK (current_step IN ('observe', 'snapshot', 'diagnose', 'propose_plan', 'safety_check', 'approval_gate', 'dry_run', 'apply', 'verify', 'measure', 'keep_rollback', 'report')),
    current_iteration INTEGER DEFAULT 1,
    max_iterations INTEGER DEFAULT 10,
    max_steps INTEGER DEFAULT 20,
    approval_timeout_hours INTEGER DEFAULT 24,
    verification_window_seconds INTEGER DEFAULT 60,
    degradation_threshold_pct NUMERIC(5,2) DEFAULT 10.0,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    last_step_transition_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    failure_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Evidence Snapshots
CREATE TABLE evidence_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id),
    host_id UUID REFERENCES hosts(id),
    evidence_type VARCHAR(30) NOT NULL CHECK (evidence_type IN ('pg_settings', 'pg_stat_database', 'pg_stat_statements', 'locks', 'replication', 'wal_checkpoint', 'os_metrics')),
    collected_at TIMESTAMPTZ NOT NULL,
    data JSONB NOT NULL,
    quality_score NUMERIC(3,2) CHECK (quality_score BETWEEN 0.0 AND 1.0),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_evidence_run_type ON evidence_snapshots(run_id, evidence_type);
CREATE INDEX idx_evidence_collected_at ON evidence_snapshots(collected_at);

-- Plans
CREATE TABLE plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id),
    host_id UUID REFERENCES hosts(id),
    status VARCHAR(30) DEFAULT 'pending_approval' CHECK (status IN ('pending_approval', 'approved', 'rejected', 'pending_forwarding', 'forwarding_failed', 'dry_run_passed', 'dry_run_failed', 'applied', 'rolled_back', 'rollback_failed', 'blocked')),
    proposed_changes JSONB NOT NULL,
    evidence_references JSONB NOT NULL,  -- [{snapshot_id, timestamp}]
    risk_score INTEGER CHECK (risk_score BETWEEN 0 AND 100),
    confidence_score NUMERIC(3,2) CHECK (confidence_score BETWEEN 0.0 AND 1.0),
    uncertainty_explanation TEXT,
    rollback_instructions JSONB NOT NULL,
    rejection_reason TEXT,
    approved_by VARCHAR(255),
    approved_at TIMESTAMPTZ,
    rejected_by VARCHAR(255),
    rejected_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    submission_time TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_plans_status ON plans(status);
CREATE INDEX idx_plans_run ON plans(run_id);
CREATE INDEX idx_plans_submission ON plans(submission_time);

-- Workload Fingerprints
CREATE TABLE workload_fingerprints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    database_name VARCHAR(63),
    name VARCHAR(120) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    status VARCHAR(30) NOT NULL,
    selection_criteria JSONB NOT NULL,
    diagnostics JSONB NOT NULL,
    observed_coverage_pct DOUBLE PRECISION NOT NULL,
    membership_stability_pct DOUBLE PRECISION,
    runtime_variance_pct DOUBLE PRECISION,
    source_snapshot_id UUID REFERENCES evidence_snapshots(id),
    source_collected_at TIMESTAMPTZ,
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE workload_fingerprint_members (
    fingerprint_id UUID NOT NULL REFERENCES workload_fingerprints(id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    query_text TEXT,
    calls BIGINT NOT NULL,
    average_query_runtime_ms DOUBLE PRECISION NOT NULL,
    total_runtime_ms DOUBLE PRECISION NOT NULL,
    runtime_coverage_pct DOUBLE PRECISION NOT NULL,
    impact_score DOUBLE PRECISION NOT NULL,
    last_seen_at TIMESTAMPTZ,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (fingerprint_id, query_id)
);

-- One immutable, comparable baseline per tuning session
CREATE TABLE baseline_measurements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL UNIQUE REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    workload_fingerprint_id UUID REFERENCES workload_fingerprints(id),
    status VARCHAR(30) NOT NULL CHECK (status IN ('ready', 'paused', 'advisory_only')),
    objective_type VARCHAR(40) NOT NULL,
    objective_formula TEXT NOT NULL,
    objective_direction VARCHAR(10) NOT NULL CHECK (objective_direction IN ('minimize', 'maximize')),
    objective_score DOUBLE PRECISION,
    metric_units JSONB NOT NULL,
    fingerprint_membership JSONB NOT NULL,
    warmup_window_seconds INTEGER NOT NULL,
    requested_measurement_window_seconds INTEGER NOT NULL,
    observed_measurement_window_seconds DOUBLE PRECISION NOT NULL,
    workload_coverage_pct DOUBLE PRECISION NOT NULL,
    runtime_variance_pct DOUBLE PRECISION,
    safety_metrics JSONB NOT NULL,
    evidence_references JSONB NOT NULL,
    root_cause_category VARCHAR(30) NOT NULL,
    root_cause_confidence DOUBLE PRECISION NOT NULL,
    root_cause_summary TEXT NOT NULL,
    root_cause_details JSONB NOT NULL,
    warnings JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Diagnostic next steps are deliberately outside the executable plan model
CREATE TABLE advisory_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    category VARCHAR(30) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    title VARCHAR(255) NOT NULL,
    summary TEXT NOT NULL,
    recommendations JSONB NOT NULL,
    evidence_references JSONB NOT NULL,
    executable BOOLEAN NOT NULL DEFAULT FALSE CHECK (executable = FALSE),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, category)
);

-- Candidate configurations measured within a tuning session
CREATE TABLE tuning_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id),
    iteration INTEGER NOT NULL,
    parameter_values JSONB NOT NULL,
    baseline_score NUMERIC,
    objective_score NUMERIC,
    best_score_before NUMERIC,
    safety_deltas JSONB NOT NULL DEFAULT '{}'::jsonb,
    workload_coverage NUMERIC(6,3),
    confidence_score NUMERIC(4,3),
    decision VARCHAR(30) CHECK (decision IN ('pending', 'kept', 'rejected', 'rolled_back', 'inconclusive')),
    measured_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(run_id, iteration)
);

-- Byte- and provenance-aware target configuration versions
CREATE TABLE configuration_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    run_id UUID REFERENCES loop_runs(id),
    backend VARCHAR(30) NOT NULL,
    status VARCHAR(30) NOT NULL CHECK (status IN ('staged', 'active', 'superseded', 'rolled_back', 'failed')),
    parameter_values JSONB NOT NULL,
    source_provenance JSONB NOT NULL,
    managed_file_checksum VARCHAR(128),
    managed_file_previous_bytes BYTEA,
    applied_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Filterable operational events; audit_log remains the immutable compliance log
CREATE TABLE host_events (
    id BIGSERIAL PRIMARY KEY,
    host_id UUID REFERENCES hosts(id),
    run_id UUID REFERENCES loop_runs(id),
    configuration_version_id UUID REFERENCES configuration_versions(id),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    component VARCHAR(50) NOT NULL,
    event_code VARCHAR(30) NOT NULL,
    message TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_candidates_run ON tuning_candidates(run_id, iteration);
CREATE INDEX idx_config_versions_host ON configuration_versions(host_id, created_at DESC);
CREATE INDEX idx_host_events_filters ON host_events(host_id, occurred_at DESC, severity, event_code);

-- Guardrail Allowlist
CREATE TABLE guardrail_allowlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    setting_name VARCHAR(255) NOT NULL,
    parameter_context VARCHAR(50) NOT NULL CHECK (parameter_context IN ('reload', 'restart')),
    max_deviation_pct NUMERIC(5,2),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(host_id, setting_name)
);

-- Audit Log (append-only)
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_type VARCHAR(20) NOT NULL CHECK (actor_type IN ('human', 'system')),
    actor_name VARCHAR(255) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    target_host_id UUID,
    result VARCHAR(20) NOT NULL CHECK (result IN ('success', 'failure', 'blocked')),
    result_reason TEXT,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Prevent UPDATE/DELETE on audit_log via DB rules
CREATE RULE no_update_audit AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE no_delete_audit AS ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE INDEX idx_audit_run ON audit_log(run_id);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_action ON audit_log(action_type);

-- DBA Reports
CREATE TABLE dba_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id) UNIQUE,
    goal TEXT NOT NULL,
    host_id UUID REFERENCES hosts(id),
    outcome_status VARCHAR(30) CHECK (outcome_status IN ('success', 'partial_success', 'failure')),
    report_content JSONB NOT NULL,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '90 days')
);

CREATE INDEX idx_reports_generated ON dba_reports(generated_at);
CREATE INDEX idx_reports_host ON dba_reports(host_id);

-- Host Agent Configuration
CREATE TABLE agent_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id) UNIQUE,
    pg_settings_interval_sec INTEGER DEFAULT 60 CHECK (pg_settings_interval_sec BETWEEN 10 AND 3600),
    pg_stats_interval_sec INTEGER DEFAULT 30 CHECK (pg_stats_interval_sec BETWEEN 5 AND 600),
    locks_replication_interval_sec INTEGER DEFAULT 15 CHECK (locks_replication_interval_sec BETWEEN 5 AND 300),
    os_metrics_interval_sec INTEGER DEFAULT 15 CHECK (os_metrics_interval_sec BETWEEN 5 AND 300),
    max_query_entries INTEGER DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Guardrail Configuration
CREATE TABLE guardrail_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    risk_threshold INTEGER DEFAULT 70 CHECK (risk_threshold BETWEEN 0 AND 100),
    dry_run_timeout_sec INTEGER DEFAULT 30,
    approval_timeout_hours INTEGER DEFAULT 24,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Key Data Transfer Objects

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from enum import Enum
from uuid import UUID

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

class ConnectionStatus(str, Enum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"

class HostSummary(BaseModel):
    id: UUID
    hostname: str
    health_status: HealthStatus
    connection_status: ConnectionStatus
    pg_version: Optional[str]
    server_role: Optional[str]
    last_heartbeat: Optional[datetime]

class WorkflowStep(str, Enum):
    OBSERVE = "observe"
    SNAPSHOT = "snapshot"
    DIAGNOSE = "diagnose"
    PROPOSE_PLAN = "propose_plan"
    SAFETY_CHECK = "safety_check"
    APPROVAL_GATE = "approval_gate"
    DRY_RUN = "dry_run"
    APPLY = "apply"
    VERIFY = "verify"
    MEASURE = "measure"
    KEEP_ROLLBACK = "keep_rollback"
    REPORT = "report"

class RunSummary(BaseModel):
    id: UUID
    goal: str
    current_step: WorkflowStep
    status: str
    current_iteration: int
    started_at: datetime
    last_step_transition_at: datetime
    elapsed_seconds: float

class EvidenceSnapshot(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    evidence_type: str
    collected_at: datetime
    data: dict
    quality_score: Optional[float]

class PlanDetail(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    status: str
    proposed_changes: List[dict]
    evidence_references: List[dict]
    risk_score: int
    confidence_score: float
    uncertainty_explanation: Optional[str]
    rollback_instructions: List[dict]
    submission_time: datetime

class RiskScore(BaseModel):
    score: int = Field(ge=0, le=100)
    breakdown: List[dict]  # Per-setting risk components
    host_role_multiplier: float
    blocked: bool
    block_reason: Optional[str]

class AuditEntry(BaseModel):
    id: int
    run_id: Optional[UUID]
    timestamp: datetime
    actor_type: str  # "human" or "system"
    actor_name: str
    action_type: str
    target_host_id: Optional[UUID]
    result: str  # "success", "failure", "blocked"
    result_reason: Optional[str]
    details: Optional[dict]

class DBAReport(BaseModel):
    id: UUID
    run_id: UUID
    goal: str
    outcome_status: str  # "success", "partial_success", "failure"
    evidence_summaries: List[dict]
    plans_proposed: List[dict]
    approval_decisions: List[dict]
    applied_changes: List[dict]
    verification_results: List[dict]
    generated_at: datetime

class AllowlistEntry(BaseModel):
    setting_name: str
    parameter_context: str  # "reload" or "restart"
    max_deviation_pct: Optional[float]
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Heartbeat-to-Status Classification

*For any* timestamp representing a last heartbeat time, the connection status classification SHALL be: "connected" if the elapsed time is less than 60 seconds, "degraded" if between 60 and 300 seconds (inclusive), and "disconnected" if greater than 300 seconds. This mapping is a total function over non-negative time deltas.

**Validates: Requirements 1.2, 2.5**

### Property 2: Health Threshold Classification

*For any* host metric value and configured threshold, the host health status SHALL transition to "unhealthy" if and only if the metric value crosses (exceeds or falls below, depending on metric type) the configured threshold.

**Validates: Requirements 1.3**

### Property 3: Evidence Categorization and Counting

*For any* set of evidence snapshots associated with a loop run, grouping by evidence_type SHALL produce category counts that sum to the total number of snapshots, with each snapshot appearing in exactly one category.

**Validates: Requirements 3.2**

### Property 4: Evidence Freshness Formatting

*For any* evidence timestamp and current time, the freshness display SHALL show seconds (e.g., "45s ago") when age < 60 seconds, minutes (e.g., "12m ago") when age < 3600 seconds, and hours (e.g., "3h ago") otherwise. The numeric value SHALL equal the floor of the age divided by the respective unit.

**Validates: Requirements 3.4**

### Property 5: Plan Queue Ordering and Pagination

*For any* set of pending plans and any page number with page size 50, the returned plans SHALL be ordered by submission_time ascending, contain at most 50 items per page, and the union of all pages SHALL equal the full set of pending plans without duplicates or omissions.

**Validates: Requirements 4.1**

### Property 6: Rejection Reason Minimum Length

*For any* string provided as a plan rejection reason, the system SHALL accept the rejection if and only if the trimmed string length is at least 10 characters. Strings shorter than 10 characters (after trimming) SHALL be rejected.

**Validates: Requirements 4.5**

### Property 7: No Execution Without Approval

*For any* plan that has reached a status beyond "approved" (i.e., "dry_run_passed", "applied", "rolled_back"), there SHALL exist a corresponding approval audit log entry with a timestamp earlier than the plan's execution timestamp. No plan SHALL reach execution state without this prerequisite.

**Validates: Requirements 4.6, 9.5**

### Property 8: Rollback Eligibility State Constraint

*For any* plan and any rollback request, the rollback SHALL be permitted if and only if the plan's current status is "applied" or "rollback_failed". For all other statuses (including "rolled_back", "pending_approval", "rejected", "blocked"), the rollback request SHALL be rejected.

**Validates: Requirements 5.4**

### Property 9: Collection Interval Range Validation

*For any* proposed collection interval configuration, the system SHALL accept the interval if and only if it falls within the permitted range for its evidence type: pg_settings [10, 3600] seconds, pg_stats [5, 600] seconds, locks/replication/WAL [5, 300] seconds, and OS metrics [5, 300] seconds. Values outside these ranges SHALL be rejected.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

### Property 10: Bounded Evidence Buffer with Chronological Ordering

*For any* sequence of evidence snapshots buffered by the Host Agent, when flushed the output SHALL be in strictly chronological order by collection timestamp. If the buffer reaches its 512 MB capacity, the oldest evidence SHALL be evicted first (FIFO), and the remaining evidence plus any new additions SHALL maintain chronological order.

**Validates: Requirements 6.6, 6.9**

### Property 11: Evidence Snapshot Structural Completeness

*For any* evidence snapshot transmitted by the Host Agent, it SHALL contain a non-null collection timestamp in UTC and a non-null host identifier. No snapshot lacking either field SHALL be accepted by the Control Plane.

**Validates: Requirements 6.7**

### Property 12: AI Evidence Grounding

*For any* recommendation produced by the AI Planning Module, every metric value referenced in the recommendation SHALL exist in or be mathematically derivable solely from the evidence snapshots collected during the current loop run. Every evidence reference (snapshot ID) in the recommendation SHALL correspond to a snapshot belonging to the current run.

**Validates: Requirements 7.1, 7.2**

### Property 13: Evidence Quality Threshold Enforcement

*For any* set of evidence where the computed quality score for a recommendation falls below the configured Evidence_Quality_Threshold, the AI Planning Module SHALL mark that recommendation as "inconclusive", list the specific insufficient evidence types, and produce zero actionable changes for that recommendation.

**Validates: Requirements 7.3**

### Property 14: Plan Rollback Instruction Completeness

*For any* plan generated by the AI Planning Module, the number of rollback instructions SHALL equal the number of proposed changes, and each rollback instruction SHALL reference the specific setting it reverses.

**Validates: Requirements 7.5**

### Property 15: Allowlist Enforcement

*For any* plan proposing PostgreSQL setting modifications and any allowlist configuration, the Guardrail Engine SHALL reject the entire plan if: (a) the allowlist is empty, OR (b) any proposed setting modification targets a setting not present in the allowlist. A plan SHALL pass allowlist checking if and only if the allowlist is non-empty AND every proposed setting is present in the allowlist.

**Validates: Requirements 8.1, 8.2**

### Property 16: Parameter Context Permission

*For any* allowlisted setting classified as "restart-required", the Guardrail Engine SHALL permit modification only when the target host has restart_required_enabled set to true. By default (restart_required_enabled = false), only "reload-safe" settings SHALL be modifiable.

**Validates: Requirements 8.3, 8.4**

### Property 17: Risk Score Calculation Bounds and Monotonicity

*For any* plan, the calculated risk score SHALL be an integer in the range [0, 100]. The score SHALL increase monotonically with: (a) the number of affected settings, (b) the percentage deviation of proposed values from current values, and (c) the host role weight (primary hosts produce higher scores than identical changes on replica hosts).

**Validates: Requirements 9.1**

### Property 18: Risk Score Threshold Blocking

*For any* plan with a calculated risk score and any configured risk threshold, the Guardrail Engine SHALL block execution if and only if the risk score strictly exceeds the threshold. Plans with risk score <= threshold SHALL not be blocked by the risk check alone.

**Validates: Requirements 9.2**

### Property 19: Rollback Plan Validation

*For any* plan and its associated rollback instructions, the Guardrail Engine SHALL validate the rollback as valid if and only if: (a) every setting modified by the plan has a corresponding restore entry in the rollback instructions, AND (b) each restore value matches the value from the pre-change settings snapshot.

**Validates: Requirements 9.4**

### Property 20: Safety Workflow Stage Ordering

*For any* plan execution trace through the Guardrail Engine, the stages SHALL occur in strict order: (1) risk scoring + allowlist check, (2) approval gate, (3) dry-run, (4) apply. If any stage fails, no subsequent stage SHALL execute. The execution trace SHALL never contain a later-stage event without all prior stages having succeeded.

**Validates: Requirements 9.7**

### Property 21: Audit Log Append-Only Integrity

*For any* existing audit log entry, attempts to update or delete that entry through any platform interface SHALL be rejected. The count of audit log entries SHALL be monotonically non-decreasing over time.

**Validates: Requirements 10.2**

### Property 22: Secret Redaction in Audit Entries

*For any* string containing patterns matching passwords (e.g., `password=...`), connection strings (e.g., `postgresql://user:pass@host`), API keys, tokens, or certificate values, the redaction function SHALL replace all detected secret content with a fixed placeholder string while preserving the surrounding non-secret structure. The output SHALL contain zero substrings matching the defined secret patterns.

**Validates: Requirements 10.3**

### Property 23: Audit Entry Chronological Ordering

*For any* query of audit log entries filtered by run_id, the returned entries SHALL be ordered by timestamp in ascending (chronological) order. For entries with identical timestamps, the ordering SHALL be stable (by insertion order / sequence ID).

**Validates: Requirements 10.5**

### Property 24: Goal Decomposition Step Limit

*For any* goal submitted to the DBA Loop Worker and any configured maximum step count, the decomposition SHALL produce a number of workflow steps less than or equal to the configured maximum (default: 20). The decomposition SHALL never produce zero steps for a non-empty goal.

**Validates: Requirements 11.1**

### Property 25: Loop Iteration Limit

*For any* loop run execution, the number of completed iterations SHALL not exceed the configured maximum (default: 10). Each iteration SHALL include at least one observation step that collects evidence before proceeding to diagnosis.

**Validates: Requirements 11.2**

### Property 26: Verification Window Range Validation

*For any* proposed verification window duration, the system SHALL accept it if and only if the value is within [10, 600] seconds. Values outside this range SHALL be rejected.

**Validates: Requirements 12.1**

### Property 27: Metric Delta Computation

*For any* pair of pre-apply and post-apply evidence values for the same metric, the per-metric delta SHALL be computed as `(post_value - pre_value) / pre_value * 100` representing percentage change. The computation SHALL handle all numeric metric types consistently.

**Validates: Requirements 12.2**

### Property 28: Degradation Threshold Decision

*For any* set of per-metric deltas and a configured degradation threshold percentage, the system SHALL initiate rollback if any single metric's degradation exceeds the threshold, and SHALL mark the change as "kept" if and only if all metrics remain within the threshold.

**Validates: Requirements 12.3, 12.4**

### Property 29: DBA Report Structural Completeness

*For any* completed loop run (whether successful, partial, or failed), the generated DBA Report SHALL contain all required sections: original goal, evidence summaries with confidence scores, all plans proposed, approval decisions, applied changes, verification results, and final outcome status. No required section SHALL be null or absent.

**Validates: Requirements 13.1**

### Property 30: Report Item Provenance Labeling

*For any* item in a DBA Report, it SHALL be labeled with exactly one of "AI_RECOMMENDATION" (for suggestions not yet validated by measurement) or "VERIFIED_FACT" (for outcomes confirmed by post-change evidence). No item SHALL have both labels or neither label.

**Validates: Requirements 13.2**

### Property 31: Report Confidence Threshold Marking

*For any* recommendation in a DBA Report whose supporting evidence confidence score is below the configured threshold, the recommendation SHALL be marked as "INCONCLUSIVE" with a reference to the specific evidence gap. Recommendations at or above the threshold SHALL NOT be marked inconclusive.

**Validates: Requirements 13.3**

### Property 32: Report Search Filtering

*For any* search query specifying date range, host identifier, and/or goal keywords, the returned reports SHALL include only reports matching ALL specified filter criteria. No report failing to match any active filter SHALL appear in results.

**Validates: Requirements 13.4**

### Property 33: Demo Mode Connection Blocking

*For any* connection attempt to a database host while Demo_Mode is active, the Control Plane SHALL reject the connection if the target address is not designated as synthetic. Zero network requests SHALL be transmitted to non-synthetic host addresses during Demo_Mode.

### Property 34: Completed Session Persistence

*For any* Tuning_Session status transition from an active to a terminal state,
the session SHALL remain discoverable through the default history query and its
workspace route SHALL resolve to the same run, Plans, Evidence, configuration
versions, events, and report.

### Property 35: Session Context Propagation

*For any* selected Tuning_Session and workspace tab, every child API request
that requires run scope SHALL use the selected route run identifier. No child
view SHALL require a separately entered identifier.

### Property 36: Candidate Measurement Comparability

*For any* two Tuning_Candidates compared in one session, workload membership,
objective formula, warm-up duration, measurement duration, and metric units
SHALL match; otherwise the comparison SHALL be marked inconclusive.

### Property 37: Candidate Keep Decision

*For any* measured candidate, a keep decision SHALL occur only when objective
improvement meets the configured minimum and every safety metric remains within
its guardrail. Every other completed measurement SHALL restore best-so-far or
baseline.

### Property 38: Parameter Catalog Disposition Completeness

*For any* completed session, every parameter supported for the target version,
platform, and selected mode SHALL appear exactly once in the final disposition
set.

### Property 39: Managed Configuration Atomicity

*For any* managed-file apply observed by PostgreSQL, the DBTune-owned file SHALL
contain either the complete previous version or the complete proposed version;
partial/truncated content SHALL never be visible at the managed path.

### Property 40: Configuration Precedence Safety

*For any* proposed managed-file setting, execution SHALL be blocked when a
higher-precedence command-line, postgres.auto.conf, later include, database,
user, or provider source controls that setting.

### Property 41: Byte-Exact Managed File Rollback

*For any* successful managed-file rollback, the resulting bytes and checksum
SHALL equal the captured pre-apply version, or the file SHALL be absent if it
was absent in the snapshot, and effective values and sourcefile SHALL match the
snapshot.

### Property 42: Duplicate Agent Write Exclusion

*For any* host identity with more than one active Host_Agent lease, all target
write operations SHALL be blocked until exactly one lease remains active and a
resolution event has been recorded.

### Property 43: Active and Referenced Evidence Preservation

*For any* Evidence snapshot attached to a non-terminal Tuning_Session, cleanup
SHALL retain the raw snapshot regardless of age. For a terminal or unattached
snapshot referenced by a Plan, baseline, advisory, candidate, or
Workload_Fingerprint, cleanup SHALL retain it until the referenced retention
cutoff.

### Property 44: Rollup-Before-Delete Atomicity

*For every* raw Evidence snapshot removed by lifecycle maintenance, exactly one
matching tenant/host/run/type/day rollup SHALL account for its snapshot count,
byte size, collection time, and optional quality score. A transaction failure
SHALL preserve both the prior rollup values and all selected raw snapshots.

### Property 45: Evidence Lifecycle Tenant Isolation

*For any* lifecycle status, preview, or cleanup request, counts, rollups, raw
rows, and maintenance history SHALL be limited to the authenticated
organization, and manual deletion SHALL require an admin principal.

**Validates: Requirements 14.4**

## Error Handling

### Error Categories and Strategies

| Category | Strategy | Example |
|----------|----------|---------|
| Host Agent Disconnection | Local buffering (512 MB max, FIFO eviction), auto-reconnect with exponential backoff | Network partition between agent and control plane |
| Evidence Collection Failure | Skip failed type, continue others, log failure | Query timeout on pg_stat_statements |
| AI Planning Module Failure | Return error to loop worker, halt iteration with audit entry | LLM API timeout or invalid response |
| Guardrail Engine Unreachable | Retry 3x at 10s intervals, mark plan "forwarding-failed" | Service crash during plan forwarding |
| Dry-Run Failure | Block plan execution, report error to DBA, log in audit | SQL parse error or timeout |
| Rollback Failure | Alert DBA with details, mark plan "rollback-failed", allow retry | Target host unreachable during rollback |
| Approval Timeout | Halt loop worker, record timeout in audit | 24-hour timeout elapses |
| Secret Redaction Failure | Block audit write, retry redaction, alert if persistent | Regex engine error on malformed input |
| Report Generation Failure | Persist raw run data, log failure, allow regeneration | Out-of-memory during large report assembly |
| Demo Mode Violation | Reject connection, log attempt, maintain demo isolation | Accidental real host address in demo config |
| Workload Coverage/Variance Failure | Pause candidate search, keep best verified version, request fresh baseline or DBA choice | Query fingerprint no longer represents the observed workload |
| Configuration Ownership Conflict | Fail preflight without mutating target; show controlling source and remediation | postgres.auto.conf overrides a DBTune-owned conf.d setting |
| Managed File Validation Failure | Preserve active file, record pg_file_settings errors, reject apply | Invalid value or syntax in rendered candidate file |
| Reload Verification Failure | Atomically restore previous version, reload, verify rollback, block session | pg_reload_conf() false or pg_settings source/value mismatch |
| Duplicate Host Agents | Block all target writes and emit coded event until one lease remains | Two agent processes report the same host identity |
| Evidence Maintenance Failure | Roll back the current batch, retain raw evidence, record a failed maintenance result, and retry on the next interval | Rollup insert or raw snapshot delete fails |

### Retry Policies

```python
RETRY_POLICIES = {
    "guardrail_forwarding": {
        "max_retries": 3,
        "interval_seconds": 10,
        "backoff": "fixed"
    },
    "evidence_collection": {
        "max_retries": 1,
        "interval_seconds": 10,
        "backoff": "fixed"
    },
    "host_agent_reconnect": {
        "max_retries": "unlimited",
        "initial_interval_seconds": 5,
        "max_interval_seconds": 300,
        "backoff": "exponential"
    },
    "audit_redaction_retry": {
        "max_retries": 3,
        "interval_seconds": 1,
        "backoff": "fixed"
    }
}
```

### Circuit Breaker Pattern

The Host Agent implements a circuit breaker for Control Plane communication:
- **Closed** (normal): All evidence transmitted immediately
- **Open** (disconnected): Evidence buffered locally, periodic probe attempts
- **Half-Open** (reconnecting): Single probe sent; if successful, flush buffer and return to Closed

### Graceful Degradation

1. **Host Agent offline**: Fleet overview shows "disconnected" status; existing evidence remains viewable
2. **AI module unavailable**: Loop worker halts at diagnosis step; DBA can manually intervene
3. **Database unavailable**: API returns 503 with retry-after header; frontend shows degraded state
4. **Redis unavailable**: WebSocket updates degrade to polling; loop worker uses database for coordination

## Testing Strategy

### Testing Pyramid

```
┌─────────────────────────────┐
│     E2E Tests (few)         │  Docker-compose based, full workflow
├─────────────────────────────┤
│   Integration Tests         │  API + DB, Host Agent + Control Plane
├─────────────────────────────┤
│   Property-Based Tests      │  Guardrails, scoring, redaction, formatting
├─────────────────────────────┤
│   Unit Tests (many)         │  Pure functions, data transformations
└─────────────────────────────┘
```

### Property-Based Testing (Hypothesis)

The platform uses **Hypothesis** (Python property-based testing library) for validating universal properties. Each property test runs a minimum of **100 iterations** with randomized inputs.

**Configuration:**
```python
from hypothesis import settings, given
from hypothesis import strategies as st

@settings(max_examples=100)
```

**Tag format for each property test:**
```python
# Feature: autonomous-postgres-dba-agent, Property {N}: {property_text}
```

Properties tested with PBT:
- **Risk score calculation** (Property 17): Random settings counts, deviations, host roles → verify bounds and monotonicity
- **Allowlist enforcement** (Property 15): Random plans × random allowlists → verify accept/reject logic
- **Secret redaction** (Property 22): Random strings with embedded secrets → verify no secrets survive
- **Evidence freshness formatting** (Property 4): Random timestamps → verify format rules
- **Heartbeat classification** (Property 1): Random time deltas → verify status mapping
- **Rollback eligibility** (Property 8): Random plan states → verify accept/reject
- **Interval validation** (Property 9): Random intervals → verify range checks
- **Plan pagination** (Property 5): Random plan sets → verify ordering and page bounds
- **Metric delta computation** (Property 27): Random numeric pairs → verify formula
- **Degradation threshold** (Property 28): Random delta sets → verify rollback decision
- **Rollback plan validation** (Property 19): Random plans and snapshots → verify completeness check
- **Safety workflow ordering** (Property 20): Random execution traces → verify stage ordering
- **Audit chronological order** (Property 23): Random audit entries → verify sort
- **Goal decomposition limit** (Property 24): Random goals → verify step count bounds

### Unit Tests

Focus areas:
- Data model serialization/deserialization
- Status enum transitions and guards
- Pagination logic
- Time formatting utilities
- Configuration validation
- Report section assembly

### Integration Tests

Focus areas:
- API endpoints with database (full request/response cycle)
- Host Agent ↔ Control Plane communication
- WebSocket event delivery
- Audit log DB rules (no update/delete enforcement)
- Demo mode seed data generation
- Docker health-check endpoints

### End-to-End Tests

Full workflow scenarios run in Docker:
1. Register host → Start loop → Collect evidence → Generate plan → Approve → Apply → Verify → Report
2. Register host → Start loop → Plan rejected → Re-plan with feedback
3. Start loop → Guardrail blocks plan → Loop halts with audit trail
4. Apply change → Verification fails → Auto-rollback triggered
5. Demo mode activation → Full workflow with synthetic data

### Test Organization

```
tests/
├── unit/
│   ├── test_risk_scoring.py
│   ├── test_allowlist.py
│   ├── test_redaction.py
│   ├── test_freshness_format.py
│   ├── test_heartbeat_status.py
│   ├── test_interval_validation.py
│   ├── test_pagination.py
│   └── test_delta_computation.py
├── property/
│   ├── test_prop_risk_score.py
│   ├── test_prop_allowlist.py
│   ├── test_prop_redaction.py
│   ├── test_prop_formatting.py
│   ├── test_prop_classification.py
│   ├── test_prop_validation.py
│   ├── test_prop_workflow_ordering.py
│   └── test_prop_buffer.py
├── integration/
│   ├── test_api_fleet.py
│   ├── test_api_runs.py
│   ├── test_api_plans.py
│   ├── test_api_audit.py
│   ├── test_host_agent.py
│   └── test_websocket.py
└── e2e/
    ├── test_full_workflow.py
    ├── test_rollback_workflow.py
    ├── test_guardrail_blocking.py
    └── test_demo_mode.py
```
