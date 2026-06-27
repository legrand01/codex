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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Loop Runs
CREATE TABLE loop_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    goal TEXT NOT NULL,
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

