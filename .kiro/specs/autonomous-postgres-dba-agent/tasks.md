# Implementation Plan: Autonomous Postgres DBA Agent Platform

## Overview

This implementation plan breaks down the Autonomous Postgres DBA Agent Platform into incremental coding tasks. The platform consists of a Python FastAPI backend, React + TypeScript frontend, PostgreSQL for state management, and Redis Streams for real-time coordination. Tasks are organized to build foundational infrastructure first, then core components, then integration and verification layers.

## Tasks

- [x] 1. Project scaffolding and core infrastructure


  - [x] 1.1 Initialize project structure and configuration files
    - Create top-level directory structure: `backend/`, `frontend/`, `host-agent/`, `docker/`, `tests/`
    - Create `backend/` with FastAPI app skeleton: `main.py`, `config.py`, `dependencies.py`
    - Create `pyproject.toml` with dependencies: fastapi, uvicorn, asyncpg, redis, pydantic, hypothesis
    - Create `frontend/` with React + TypeScript Vite project scaffold
    - Create `docker-compose.yml` with services: app, postgres, redis
    - Create `Dockerfile` for backend, `Dockerfile` for frontend
    - _Requirements: 15.1, 15.2, 15.3_

  - [x] 1.2 Create database schema and migration files
    - Create SQL migration file with all tables: hosts, loop_runs, evidence_snapshots, plans, guardrail_allowlist, audit_log, dba_reports, agent_config, guardrail_config
    - Include all CHECK constraints, indexes, and the append-only rules for audit_log
    - Create a database initialization script that applies migrations on startup
    - _Requirements: 10.2, 6.1, 6.2, 6.3, 6.4, 9.1_

  - [x] 1.3 Implement core data models and Pydantic schemas
    - Create `backend/models/` with Pydantic models: HostSummary, RunSummary, EvidenceSnapshot, PlanDetail, RiskScore, AuditEntry, DBAReport, AllowlistEntry
    - Create enum classes: HealthStatus, ConnectionStatus, WorkflowStep, PlanStatus
    - Create shared configuration models: LoopConfig, AgentConfig, GuardrailConfig
    - _Requirements: 1.1, 2.1, 3.1, 4.1_


  - [x] 1.4 Implement database connection pool and repository base
    - Create `backend/db/` with asyncpg connection pool setup
    - Create base repository class with common CRUD patterns
    - Create Redis connection manager for Streams and pub/sub
    - Implement health-check endpoint at `/health` returning HTTP 200
    - _Requirements: 15.1, 15.2_

  - [ ]* 1.5 Write unit tests for data models and configuration validation
    - Test Pydantic model serialization/deserialization
    - Test enum transitions and value constraints
    - Test configuration validation (interval ranges, threshold bounds)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 9.1_

- [x] 2. Fleet management and host registration

  - [x] 2.1 Implement Fleet API endpoints
    - Create `backend/api/fleet.py` with routes: GET `/api/v1/fleet/` (list hosts), GET `/api/v1/fleet/{host_id}` (get host), POST `/api/v1/fleet/` (register host)
    - Implement host registration with hostname, pg_version, server_role
    - Implement connection status derivation based on last_heartbeat (connected < 60s, degraded 60-300s, disconnected > 300s)
    - Return empty-state response when no hosts are registered
    - _Requirements: 1.1, 1.2, 1.4, 1.5_


  - [x] 2.2 Implement heartbeat processing and health status logic
    - Create `backend/services/fleet_service.py` with heartbeat reception
    - Implement connection status classification function (connected/degraded/disconnected based on heartbeat age)
    - Implement health threshold crossing detection with configurable thresholds
    - Update host health_status within 30 seconds of metric threshold crossing
    - _Requirements: 1.2, 1.3_

  - [ ]* 2.3 Write property test for heartbeat-to-status classification
    - **Property 1: Heartbeat-to-Status Classification**
    - Generate random non-negative time deltas and verify classification: <60s → connected, 60-300s → degraded, >300s → disconnected
    - **Validates: Requirements 1.2, 2.5**

  - [ ]* 2.4 Write property test for health threshold classification
    - **Property 2: Health Threshold Classification**
    - Generate random metric values and thresholds, verify status transitions occur if and only if threshold is crossed
    - **Validates: Requirements 1.3**

  - [x] 2.5 Implement WebSocket for fleet status updates
    - Create `backend/api/ws_fleet.py` with WebSocket endpoint at `/ws/fleet`
    - Push real-time updates when host status changes (health, connection status)
    - Implement Redis pub/sub integration for cross-worker event distribution
    - _Requirements: 1.3, 2.2_


- [~] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Audit logging and secrets redaction

  - [~] 4.1 Implement Audit Logger service
    - Create `backend/services/audit_logger.py` with append-only logging
    - Implement `log(entry: AuditEntry)` that persists structured entries within 5 seconds
    - Implement `query(run_id, time_range)` returning entries in chronological order
    - Enforce audit entry structure: ISO 8601 timestamp, actor_type (human/system), actor_name, action_type, target_host_id, result (success/failure/blocked), result_reason
    - _Requirements: 10.1, 10.2, 10.4, 10.5_

  - [~] 4.2 Implement secrets redaction function
    - Create `backend/services/redaction.py` with `redact_secrets(content: str) -> str`
    - Implement regex patterns for: passwords, connection strings, Base64 tokens, API keys
    - Replace detected secrets with fixed placeholder `[REDACTED]`
    - Implement redaction-failure handling: block write, log alert, retry before persisting
    - _Requirements: 10.3, 10.6_

  - [ ]* 4.3 Write property test for secret redaction
    - **Property 22: Secret Redaction in Audit Entries**
    - Generate random strings with embedded password=, postgresql://, API key, and token patterns
    - Verify output contains zero substrings matching secret patterns while preserving surrounding structure
    - **Validates: Requirements 10.3**


  - [ ]* 4.4 Write property test for audit log append-only integrity
    - **Property 21: Audit Log Append-Only Integrity**
    - Verify that audit entry count is monotonically non-decreasing and that update/delete operations are rejected
    - **Validates: Requirements 10.2**

  - [ ]* 4.5 Write property test for audit chronological ordering
    - **Property 23: Audit Entry Chronological Ordering**
    - Generate random audit entries with various timestamps, verify query results are ordered by timestamp ascending, with stable ordering for identical timestamps
    - **Validates: Requirements 10.5**

- [ ] 5. Host Agent evidence collection

  - [~] 5.1 Implement Host Agent core service
    - Create `host-agent/agent.py` with configurable collection intervals
    - Implement evidence collectors: `collect_pg_settings()`, `collect_pg_stats()`, `collect_locks()`, `collect_replication()`, `collect_wal_checkpoint()`, `collect_os_metrics()`
    - Implement heartbeat reporting to Control Plane (periodic HTTP POST)
    - Implement role/version detection on startup and role change
    - Include UTC collection timestamp and host identifier with every snapshot
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.7_


  - [~] 5.2 Implement local evidence buffering and flush logic
    - Create `host-agent/buffer.py` with local file-based buffer (max 512 MB)
    - Implement FIFO eviction when buffer reaches capacity
    - Implement chronological flush on reconnection (within 30 seconds)
    - Implement circuit breaker pattern: Closed → Open → Half-Open states
    - _Requirements: 6.6, 6.8, 6.9_

  - [ ]* 5.3 Write property test for evidence buffer ordering
    - **Property 10: Bounded Evidence Buffer with Chronological Ordering**
    - Generate random evidence sequences, verify flush output is strictly chronological, and verify FIFO eviction at 512 MB capacity
    - **Validates: Requirements 6.6, 6.9**

  - [ ]* 5.4 Write property test for evidence snapshot structural completeness
    - **Property 11: Evidence Snapshot Structural Completeness**
    - Generate random evidence payloads, verify every snapshot contains non-null UTC timestamp and non-null host identifier
    - **Validates: Requirements 6.7**

  - [ ]* 5.5 Write property test for collection interval validation
    - **Property 9: Collection Interval Range Validation**
    - Generate random interval values for each evidence type, verify acceptance only within permitted ranges: pg_settings [10, 3600], pg_stats [5, 600], locks/replication/WAL [5, 300], OS metrics [5, 300]
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**


  - [~] 5.6 Implement Evidence API endpoints
    - Create `backend/api/evidence.py` with routes: GET `/api/v1/evidence/{run_id}` (list evidence by run), GET `/api/v1/evidence/snapshot/{snapshot_id}` (get specific snapshot)
    - Implement evidence categorization by type with snapshot counts per category
    - Implement evidence freshness age formatting (seconds < 60s, minutes < 3600s, hours otherwise)
    - Return empty-state when no evidence exists for a run
    - Handle unavailable evidence references with disabled link state
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 5.7 Write property test for evidence freshness formatting
    - **Property 4: Evidence Freshness Formatting**
    - Generate random timestamp pairs, verify display shows seconds when age < 60, minutes when age < 3600, hours otherwise, with correct floor division
    - **Validates: Requirements 3.4**

  - [ ]* 5.8 Write property test for evidence categorization
    - **Property 3: Evidence Categorization and Counting**
    - Generate random evidence snapshot sets, verify grouping by type produces counts summing to total, with each snapshot in exactly one category
    - **Validates: Requirements 3.2**

- [~] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.


- [ ] 7. Guardrail Engine implementation

  - [~] 7.1 Implement allowlist enforcement
    - Create `backend/services/guardrail_engine.py` with `check_allowlist(plan, host_id) -> AllowlistResult`
    - Reject entire plan if allowlist is empty or any proposed setting is not in allowlist
    - Classify settings as reload-safe or restart-required based on parameter_context
    - Permit restart-required changes only when host has restart_required_enabled = true
    - Record violations in Audit_Log with disallowed setting names and host identifier
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 7.2 Write property test for allowlist enforcement
    - **Property 15: Allowlist Enforcement**
    - Generate random plans and random allowlist configurations, verify rejection when allowlist is empty or any setting is not in allowlist, and acceptance only when non-empty and all settings present
    - **Validates: Requirements 8.1, 8.2**

  - [ ]* 7.3 Write property test for parameter context permission
    - **Property 16: Parameter Context Permission**
    - Generate random settings with reload/restart context and host configurations, verify restart-required modifications are permitted only when explicitly enabled
    - **Validates: Requirements 8.3, 8.4**


  - [~] 7.4 Implement risk score calculation
    - Implement `calculate_risk_score(plan, host) -> RiskScore` in guardrail_engine.py
    - Calculate score based on: number of affected settings, percentage deviation from current values, host role weight (primary=1.5, replica=1.0)
    - Clamp result to [0, 100] range
    - Block execution when score exceeds configurable threshold (default: 70)
    - Record block decisions in Audit_Log
    - _Requirements: 9.1, 9.2_

  - [ ]* 7.5 Write property test for risk score bounds and monotonicity
    - **Property 17: Risk Score Calculation Bounds and Monotonicity**
    - Generate random setting counts, deviations, and host roles, verify score is in [0, 100], increases with more settings, larger deviations, and primary hosts
    - **Validates: Requirements 9.1**

  - [ ]* 7.6 Write property test for risk score threshold blocking
    - **Property 18: Risk Score Threshold Blocking**
    - Generate random risk scores and thresholds, verify blocking if and only if score strictly exceeds threshold
    - **Validates: Requirements 9.2**

  - [~] 7.7 Implement dry-run execution
    - Implement `execute_dry_run(plan, host_id, timeout=30) -> DryRunResult`
    - Verify SQL statements parse correctly and target settings exist in host's pg_settings
    - Implement configurable timeout with blocking on failure or timeout
    - Record dry-run results in Audit_Log
    - _Requirements: 9.3, 9.6_


  - [~] 7.8 Implement rollback plan validation
    - Implement `validate_rollback_plan(plan, pre_snapshot) -> RollbackValidation`
    - Validate: every modified setting has a restore entry, and each restore value matches pre-change snapshot
    - Block execution if rollback plan is invalid
    - _Requirements: 9.4_

  - [ ]* 7.9 Write property test for rollback plan validation
    - **Property 19: Rollback Plan Validation**
    - Generate random plans and pre-change snapshots, verify validation passes only when every modified setting has a matching restore value
    - **Validates: Requirements 9.4**

  - [~] 7.10 Implement safety workflow orchestration
    - Implement `full_safety_check(plan, host_id) -> SafetyCheckResult`
    - Enforce strict ordering: risk scoring + allowlist → approval gate → dry-run → apply
    - If any stage fails, halt and do not proceed to subsequent stages
    - Record each stage result in Audit_Log
    - _Requirements: 9.5, 9.7_

  - [ ]* 7.11 Write property test for safety workflow ordering
    - **Property 20: Safety Workflow Stage Ordering**
    - Generate random execution traces, verify stages occur in strict order and no later stage executes without prior stages succeeding
    - **Validates: Requirements 9.7**


- [ ] 8. Plan review and approval queue

  - [~] 8.1 Implement Plans API endpoints
    - Create `backend/api/plans.py` with routes: GET `/api/v1/plans/` (list pending, paginated), GET `/api/v1/plans/{plan_id}` (get detail), POST `/api/v1/plans/{plan_id}/approve`, POST `/api/v1/plans/{plan_id}/reject`
    - Implement pagination with max 50 plans per page, ordered by submission_time ascending
    - Display proposed changes, evidence references, risk score, uncertainty explanations, rollback instructions
    - Load plan detail within 3 seconds of selection
    - _Requirements: 4.1, 4.2_

  - [~] 8.2 Implement plan approval workflow
    - On approval: forward plan to Guardrail Engine for dry-run, record approval in Audit_Log with timestamp and DBA identity
    - Implement retry logic: if Guardrail Engine unreachable within 30s, retain as "pending-forwarding", retry 3 times at 10s intervals, then mark "forwarding-failed"
    - Enforce: no plan proceeds to execution without explicit DBA approval in Audit_Log
    - _Requirements: 4.3, 4.4, 4.6_

  - [~] 8.3 Implement plan rejection workflow
    - On rejection: require reason of at least 10 characters (trimmed), record rejection in Audit_Log, notify DBA_Loop_Worker to re-plan with rejection feedback
    - Validate rejection reason minimum length server-side
    - _Requirements: 4.5_


  - [ ]* 8.4 Write property test for plan queue ordering and pagination
    - **Property 5: Plan Queue Ordering and Pagination**
    - Generate random plan sets, verify ordering by submission_time, max 50 per page, union of all pages equals full set without duplicates
    - **Validates: Requirements 4.1**

  - [ ]* 8.5 Write property test for rejection reason minimum length
    - **Property 6: Rejection Reason Minimum Length**
    - Generate random strings, verify acceptance if and only if trimmed length >= 10 characters
    - **Validates: Requirements 4.5**

  - [ ]* 8.6 Write property test for no execution without approval
    - **Property 7: No Execution Without Approval**
    - Generate random plan state histories, verify any plan beyond "approved" state has a corresponding approval audit entry with earlier timestamp
    - **Validates: Requirements 4.6, 9.5**

- [~] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Rollback controls

  - [~] 10.1 Implement Rollback API endpoints
    - Create `backend/api/rollback.py` with routes: POST `/api/v1/rollback/{plan_id}` (initiate), GET `/api/v1/rollback/{plan_id}/status` (get status)
    - Execute rollback instructions stored with original plan
    - Complete or fail within 300 seconds timeout
    - Display status: pending, in-progress, completed, or failed (update within 5 seconds of transitions)
    - _Requirements: 5.1, 5.2_


  - [~] 10.2 Implement rollback eligibility and error handling
    - Allow rollback only for plans with status "applied" or "rollback-failed"
    - Prevent rollback for plans with status "rolled-back"
    - On failure: alert DBA with failure details (step that failed, error), preserve Audit_Log entry, mark plan as eligible for retry
    - Reject rollback if instructions are missing or unparsable, alert DBA with error
    - On success: transition plan to "rolled-back", record outcome in Audit_Log
    - _Requirements: 5.3, 5.4, 5.5, 5.6_

  - [ ]* 10.3 Write property test for rollback eligibility state constraint
    - **Property 8: Rollback Eligibility State Constraint**
    - Generate random plan statuses, verify rollback permitted only for "applied" or "rollback_failed", rejected for all other statuses
    - **Validates: Requirements 5.4**

- [ ] 11. AI Planning Module

  - [~] 11.1 Implement AI Planning Module core
    - Create `backend/services/ai_planning.py` with `diagnose(evidence, goal) -> DiagnosisResult` and `generate_plan(diagnosis, evidence, current_settings, rejection_feedback) -> Plan`
    - Integrate with OpenAI-compatible LLM API for structured output
    - Implement evidence grounding: reference only evidence from current loop run, never fabricate metrics
    - Include confidence score [0.0, 1.0] and evidence gaps for each recommendation
    - _Requirements: 7.1, 7.2, 7.4, 7.6_


  - [~] 11.2 Implement evidence quality checking
    - Implement `check_evidence_quality(evidence) -> EvidenceQualityReport`
    - Mark recommendations as inconclusive when evidence quality is insufficient
    - List specific evidence types that are missing or insufficient
    - Omit actionable changes for inconclusive recommendations
    - If all evidence is below threshold or empty, return plan with only diagnostic summary
    - _Requirements: 7.3, 7.7_

  - [~] 11.3 Implement rollback instruction generation
    - Ensure every proposed change has a corresponding reversal action in the plan
    - Include evidence references (snapshot IDs and timestamps) for each recommendation
    - Generate plans that are executable by the Control Plane without additional DBA input
    - _Requirements: 7.5, 7.6_

  - [ ]* 11.4 Write property test for plan rollback instruction completeness
    - **Property 14: Plan Rollback Instruction Completeness**
    - Generate random plans, verify rollback instruction count equals proposed change count, and each instruction references the specific setting it reverses
    - **Validates: Requirements 7.5**

- [ ] 12. DBA Loop Worker

  - [~] 12.1 Implement DBA Loop Worker core execution engine
    - Create `backend/services/loop_worker.py` with `start_run(goal, config) -> RunResult`
    - Implement goal decomposition into observe/diagnose/plan/verify steps (max configurable steps, default 20)
    - Implement iterative loop execution (max configurable iterations, default 10)
    - Collect new evidence at each observation step before proceeding to diagnosis
    - _Requirements: 11.1, 11.2_


  - [~] 12.2 Implement approval gate and guardrail integration
    - Submit plans requiring database modification to Guardrail Engine
    - Pause execution until Approval_Gate is resolved or approval timeout (default 24h) elapses
    - On guardrail rejection: stop execution, record failure reason in Audit_Log
    - On approval timeout: halt execution, record timeout in Audit_Log
    - _Requirements: 11.3, 11.4, 11.7_

  - [~] 12.3 Implement loop halt and error handling
    - Implement `halt_run(run_id)` to stop active runs within 10 seconds
    - Transition status to "manually_halted", preserve completed step state
    - On evidence collection failure: retry once after 10 seconds, halt if retry fails
    - On max iteration reached without goal: halt and generate report indicating limit
    - Mark runs as "unresponsive" if no step transition or heartbeat within 60 seconds
    - Reject halt on completed/stopped runs with appropriate message
    - _Requirements: 2.4, 2.5, 2.6, 11.8, 11.9_

  - [ ]* 12.4 Write property test for goal decomposition limit
    - **Property 24: Goal Decomposition Step Limit**
    - Generate random goals and max step configurations, verify decomposition produces steps <= configured max and > 0 for non-empty goals
    - **Validates: Requirements 11.1**

  - [ ]* 12.5 Write property test for loop iteration limit
    - **Property 25: Loop Iteration Limit**
    - Verify number of completed iterations never exceeds configured maximum, and each iteration includes at least one observation step
    - **Validates: Requirements 11.2**


  - [~] 12.6 Implement Runs API endpoints
    - Create `backend/api/runs.py` with routes: POST `/api/v1/runs/` (start run), POST `/api/v1/runs/{run_id}/halt` (halt run), GET `/api/v1/runs/{run_id}` (get status), GET `/api/v1/runs/` (list active runs)
    - Display: run ID, goal, current workflow step, elapsed time, last step transition
    - Implement WebSocket at `/ws/runs/{run_id}` for real-time step transition updates (within 5 seconds)
    - Display guardrail violation details when loop is stopped by guardrail failure
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [ ] 13. Post-apply verification and rollback decision

  - [~] 13.1 Implement post-apply verification logic
    - Create `backend/services/verification.py` with verification evidence collection
    - Collect verification evidence within configurable observation window (10-600s, default 60s)
    - Compare pre-apply and post-apply evidence for same metric categories
    - Compute per-metric delta as `(post - pre) / pre * 100`
    - _Requirements: 12.1, 12.2_

  - [~] 13.2 Implement automatic rollback decision
    - If any metric degrades beyond configurable threshold (default 10%): initiate rollback, record triggering metric, delta, and threshold in Audit_Log
    - If all metrics within threshold: mark change as "kept", proceed to next step
    - If verification evidence collection fails: initiate rollback, record failure reason in Audit_Log
    - _Requirements: 12.3, 12.4, 12.5_


  - [ ]* 13.3 Write property test for verification window range validation
    - **Property 26: Verification Window Range Validation**
    - Generate random duration values, verify acceptance only within [10, 600] seconds
    - **Validates: Requirements 12.1**

  - [ ]* 13.4 Write property test for metric delta computation
    - **Property 27: Metric Delta Computation**
    - Generate random pre/post numeric pairs, verify delta = (post - pre) / pre * 100 for all numeric types
    - **Validates: Requirements 12.2**

  - [ ]* 13.5 Write property test for degradation threshold decision
    - **Property 28: Degradation Threshold Decision**
    - Generate random delta sets and thresholds, verify rollback if any single metric exceeds threshold, kept only if all metrics within threshold
    - **Validates: Requirements 12.3, 12.4**

- [~] 14. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 15. DBA Report generation

  - [~] 15.1 Implement DBA Report generation
    - Create `backend/services/report_generator.py` with `generate_report(run_id) -> DBAReport`
    - Generate report within 30 seconds containing: original goal, evidence summaries with confidence scores, plans proposed, approval decisions, applied changes, verification results, outcome status
    - Label each item as "AI_RECOMMENDATION" or "VERIFIED_FACT"
    - Mark recommendations with evidence below confidence threshold as "INCONCLUSIVE" with evidence gap reference
    - _Requirements: 13.1, 13.2, 13.3_


  - [~] 15.2 Implement Reports API and search
    - Create `backend/api/reports.py` with routes: GET `/api/v1/reports/{run_id}` (get report), GET `/api/v1/reports/search` (search reports)
    - Implement search by date range, host identifier, and goal keywords (return within 5 seconds)
    - Retain reports for minimum 90 days (enforce via expires_at column)
    - Handle report generation failure: log failure, persist raw run data for regeneration
    - _Requirements: 13.4, 13.5, 13.6_

  - [ ]* 15.3 Write property test for DBA report structural completeness
    - **Property 29: DBA Report Structural Completeness**
    - Generate random completed loop runs, verify report contains all required sections (goal, evidence summaries, plans, approvals, changes, verification results, outcome status) with no null/absent section
    - **Validates: Requirements 13.1**

  - [ ]* 15.4 Write property test for report item provenance labeling
    - **Property 30: Report Item Provenance Labeling**
    - Generate random report items, verify each is labeled with exactly one of "AI_RECOMMENDATION" or "VERIFIED_FACT"
    - **Validates: Requirements 13.2**

  - [ ]* 15.5 Write property test for report confidence threshold marking
    - **Property 31: Report Confidence Threshold Marking**
    - Generate random recommendations with confidence scores, verify those below threshold are marked "INCONCLUSIVE" and those at/above are not
    - **Validates: Requirements 13.3**


  - [ ]* 15.6 Write property test for report search filtering
    - **Property 32: Report Search Filtering**
    - Generate random reports and filter queries, verify results include only reports matching ALL specified criteria
    - **Validates: Requirements 13.4**

- [ ] 16. Demo Mode implementation

  - [~] 16.1 Implement Demo Mode activation and seed data
    - Create `backend/services/demo_mode.py` with demo data seeding logic
    - Seed fleet with at least 3 hosts: one connected, one degraded, one disconnected; at least one healthy, one unhealthy
    - Generate synthetic evidence: slow queries, config drift, replication lag, checkpoint pressure, weak-evidence cases
    - Execute loops against synthetic data producing at least one successful and one blocked/inconclusive outcome
    - Generate at least one plan requiring Approval_Gate interaction
    - _Requirements: 14.1, 14.2, 14.3, 14.6_

  - [~] 16.2 Implement Demo Mode isolation and connection blocking
    - Block any connection attempts to real database hosts while Demo_Mode is active
    - Reject network requests to non-synthetic host addresses
    - Create `backend/api/demo.py` with route: POST `/api/v1/demo/activate`, POST `/api/v1/demo/deactivate`
    - _Requirements: 14.4_


  - [ ]* 16.3 Write property test for Demo Mode connection blocking
    - **Property 33: Demo Mode Connection Blocking**
    - Generate random connection attempts with synthetic and non-synthetic addresses, verify all non-synthetic connections are rejected and zero requests are transmitted
    - **Validates: Requirements 14.4**

- [ ] 17. React frontend implementation

  - [~] 17.1 Implement frontend project structure and shared components
    - Set up React + TypeScript + Vite with routing (React Router)
    - Create shared UI components: StatusBadge, DataTable, EmptyState, LoadingSpinner, Pagination
    - Set up WebSocket client utility for real-time updates
    - Set up API client with typed request/response handlers
    - Create persistent Demo Mode indicator banner component
    - _Requirements: 14.5, 15.3_

  - [~] 17.2 Implement Fleet Overview page
    - Create FleetOverview component displaying all registered hosts
    - Show: hostname, health status, connection status, PostgreSQL version, server role
    - Implement real-time health status updates via WebSocket (within 30 seconds of metric change)
    - Display empty-state when no hosts registered
    - Connect to `/ws/fleet` for live updates
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_


  - [~] 17.3 Implement Active Runs monitoring page
    - Create ActiveRuns component displaying all active loop runs
    - Show: run ID, goal, current step, elapsed time, last step transition
    - Implement real-time step updates via WebSocket (within 5 seconds of transition)
    - Display guardrail violation details for stopped runs
    - Show "unresponsive" indicator for runs with no activity > 60 seconds
    - Implement halt button with confirmation, handle halt on non-active runs
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [~] 17.4 Implement Evidence Viewer page
    - Create EvidenceViewer component with categorized evidence display
    - Show evidence categorized by type with snapshot counts
    - Implement navigable links from plans to referenced evidence snapshots
    - Display evidence freshness age (seconds/minutes/hours), updated every 30 seconds
    - Handle empty-state (no evidence) and unavailable evidence (disabled link state)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [~] 17.5 Implement Plan Approval Queue page
    - Create ApprovalQueue component with paginated plan list (max 50 per page)
    - Show plan details: proposed changes, evidence refs, risk score, uncertainty, rollback instructions
    - Implement approve button (triggers dry-run flow)
    - Implement reject button with mandatory reason input (min 10 chars)
    - Display forwarding status and error states
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_


  - [~] 17.6 Implement Rollback Controls and Audit Log pages
    - Create RollbackControls component: initiate rollback, display status (pending/in-progress/completed/failed)
    - Show rollback eligibility based on plan status (only "applied" or "rollback-failed")
    - Create AuditLog component: display entries in chronological order for a given run
    - Show: timestamp, actor, action type, target host, result
    - Create ReportsViewer component: display/search DBA reports by date, host, keywords
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 10.4, 10.5, 13.4_

- [~] 18. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 19. Integration wiring and end-to-end flows

  - [~] 19.1 Wire DBA Loop Worker with all services
    - Connect Loop Worker → Host Agent (evidence collection requests)
    - Connect Loop Worker → AI Planning Module (diagnosis and plan generation)
    - Connect Loop Worker → Guardrail Engine (safety checks)
    - Connect Loop Worker → Audit Logger (decision logging throughout execution)
    - Connect Loop Worker → Report Generator (final report generation)
    - Implement Redis Streams for real-time coordination between loop worker and API
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_


  - [~] 19.2 Wire frontend to backend API and WebSocket
    - Connect all React pages to corresponding API endpoints
    - Connect fleet page to `/ws/fleet` WebSocket
    - Connect active runs page to `/ws/runs/{run_id}` WebSocket
    - Implement error handling and loading states across all pages
    - Wire Demo Mode indicator to backend demo status
    - _Requirements: 1.1, 2.2, 14.5_

  - [ ]* 19.3 Write integration tests for API + database flows
    - Test fleet registration and heartbeat processing
    - Test plan approval/rejection with audit log recording
    - Test rollback initiation and status transitions
    - Test evidence storage and retrieval
    - Test WebSocket event delivery for status changes
    - _Requirements: 1.1, 4.3, 5.1, 10.1_

  - [ ]* 19.4 Write integration tests for Host Agent communication
    - Test Host Agent heartbeat reporting and evidence submission
    - Test buffering behavior on connection loss and chronological flush on reconnection
    - Test evidence rejection when missing timestamp or host ID
    - _Requirements: 6.5, 6.6, 6.7_


- [ ] 20. Deployment, Docker, and documentation

  - [~] 20.1 Finalize Docker and docker-compose configuration
    - Create production-ready Dockerfile with health-check (HTTP 200 within 30s)
    - Configure docker-compose.yml: app, postgres, redis services (ready within 60s)
    - Create local development setup script: install deps, start with hot-reload, confirm HTTP readiness within 60s
    - Handle startup failures with non-zero exit code and component failure message
    - _Requirements: 15.1, 15.2, 15.3, 15.7_

  - [~] 20.2 Create automated test suite runner
    - Create test runner script executing: guardrail enforcement tests, loop execution tests, evidence collection tests, plan generation tests
    - Ensure test suite exits with zero exit code when all tests pass
    - Configure pytest with Hypothesis settings (max_examples=100 for property tests)
    - _Requirements: 15.5_

  - [~] 20.3 Create README documentation
    - Write architecture overview section
    - Write step-by-step local development setup instructions
    - Document all required environment variables with descriptions and example values
    - Write demo walkthrough section with numbered steps exercising full plan-generation-to-execution workflow
    - _Requirements: 15.4_

  - [~] 20.4 Create deployment script
    - Create deployment script that makes the platform accessible via web browser at configurable host/port
    - Return valid HTTP response within 30 seconds of script completion
    - _Requirements: 15.6_


- [~] 21. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 22. Persistent tuning-session product workflow

  - [ ] 22.1 Replace active-only run listing with persistent session history
    - Add paginated/filterable all-status runs API while retaining an active-only filter
    - Include completion timestamp and freeze terminal-run elapsed duration
    - Add indexes for host, status, objective, mode, and start/completion date filters
    - Preserve direct detail access for completed, failed, halted, and timed-out sessions
    - _Requirements: 16.1, 16.2, 16.6, 16.7_

  - [ ] 22.2 Implement Start tuning flow
    - Add primary Start tuning action from Fleet and Tuning pages
    - Select host/database, objective, Workload_Fingerprint, mode, parameters, approval policy, measurement windows, and guardrails
    - Run capability/preflight checks before enabling session creation
    - _Requirements: 16.1, 17.1, 18.5, 18.6, 18.7_

  - [ ] 22.3 Implement the session-centric React workspace
    - Create `/tuning/:runId` with persistent session header
    - Add Overview, Configuration, Workload, Evidence, Activity, and Report tabs
    - Pass runId from route context to every child request; remove UUID entry forms from normal navigation
    - Link session history, plans, evidence, events, configuration versions, rollback, and reports bidirectionally
    - Keep Plans as a queue filter inside Tuning/Administration rather than a disconnected history page
    - _Requirements: 16.3, 16.4, 16.5, 16.8_

  - [ ]* 22.4 Test session persistence and context propagation
    - Property 34: terminal sessions remain discoverable
    - Property 35: every workspace child uses the route runId
    - End-to-end test: start session, approve, complete, revisit from history, open all tabs without UUID entry
    - _Requirements: 16.2, 16.3, 16.4, 16.8_

- [ ] 23. Workload fingerprints and measured candidate optimization

  - [ ] 23.1 Implement Workload_Fingerprint storage and APIs
    - Capture normalized query ID, optional query text, AQR, calls, total duration, runtime coverage, and last seen
    - Implement recommended fingerprint generation and named custom fingerprints
    - Detect low coverage, unstable membership, and high measurement variance
    - _Requirements: 17.2, 17.3, 17.4, 17.9_

  - [ ] 23.2 Implement baseline measurement protocol
    - Persist objective formula, fingerprint membership, warm-up window, measurement window, units, workload coverage, and safety metrics
    - Add a root-cause gate for configuration, query/index, lock, vacuum/bloat, resource saturation, connection pressure, and insufficient evidence
    - Route query/index findings to a separate non-executable advisory track instead of using parameter changes as a universal answer
    - Reject or pause baseline when coverage or variance fails configured thresholds
    - _Requirements: 17.5, 17.9, 17.10_

  - [ ] 23.3 Implement bounded CandidateOptimizer
    - Introduce deterministic bounded-search strategy behind CandidateOptimizer interface
    - Generate candidates only within versioned per-setting domains and guardrail allowlists
    - Apply, warm up, measure, score, and compare every candidate against baseline and best-so-far
    - Keep only beneficial/safe candidates; otherwise restore best or exact baseline
    - Persist all candidate measurements and decisions
    - _Requirements: 17.6, 17.7, 17.8_

  - [ ] 23.4 Implement supported parameter catalog and dispositions
    - Version catalog by PostgreSQL major version and platform
    - Add the 15 reload-only settings and four restart-enabled settings specified in Requirement 18
    - Display current/source/context/pending restart, baseline, best, pending, and final disposition for every catalog entry
    - _Requirements: 18.1, 18.2, 18.3, 18.4_

  - [ ]* 23.5 Test candidate correctness
    - Property 36: measurement comparability
    - Property 37: keep only objective improvement within all guardrails
    - Property 38: complete one-to-one final parameter dispositions
    - Add noisy-workload, low-coverage, regression, and convergence integration cases
    - _Requirements: 17.6, 17.7, 17.8, 17.9, 18.4_

- [ ] 24. Pluggable configuration backends and managed conf.d ownership

  - [ ] 24.1 Define ConfigurationBackend protocol and router
    - Implement common preflight, snapshot, apply, rollback, and verification result models
    - Select backend from explicit per-host enrollment and platform capability
    - Record backend in sessions, plans, configuration versions, audit entries, and events
    - _Requirements: 19.1, 19.10_

  - [ ] 24.2 Retain and generalize AlterSystemBackend
    - Preserve PostgreSQL 15+ parameter-scoped ALTER SYSTEM and pg_reload_conf path
    - Verify source/provenance and exact snapshot restoration
    - Support the complete reload-only catalog and staged restart parameters
    - _Requirements: 18.1, 18.2, 19.2, 19.9_

  - [ ] 24.3 Implement self-managed ManagedConfFileBackend in Host_Agent
    - Enroll only verified self-managed hosts with configured managed path
    - Verify config_file/include_dir ordering, permissions, ownership, disk space, and same-filesystem rename
    - Detect higher-precedence command-line, postgres.auto.conf, later-include, database/user, and provider conflicts
    - Render allowlisted settings to `99-dbtune-managed.conf`
    - Atomic temp-write + fsync + pg_file_settings validation + rename + pg_reload_conf
    - Verify effective value and sourcefile
    - _Requirements: 19.3, 19.4, 19.5, 19.6_

  - [ ] 24.4 Implement byte-exact managed-file rollback and recovery
    - Persist checksum, exact prior bytes/absence, owner, mode, and target values before apply
    - Restore previous bytes or remove file atomically on rollback/failure
    - Reload and verify both value and provenance
    - Reconcile interrupted apply operations from durable configuration-version state
    - _Requirements: 19.7, 19.8, 19.9_

  - [ ] 24.5 Add provider-managed configuration adapter interface
    - Define staged apply/reboot/poll/verify contract for RDS/Aurora, Cloud SQL, Aiven, and future providers
    - Do not emulate file access on managed services
    - _Requirements: 19.1, 19.10_

  - [ ]* 24.6 Test configuration ownership safety
    - Property 39: managed-file atomicity
    - Property 40: higher-precedence conflict blocks apply
    - Property 41: byte-exact rollback and provenance restoration
    - Real PostgreSQL tests for include_dir, postgres.auto.conf conflict, invalid pg_file_settings, reload failure, and restart-pending state
    - _Requirements: 19.4, 19.5, 19.6, 19.7, 19.8, 19.9_

- [ ] 25. Configuration history, agent capabilities, and coded events

  - [ ] 25.1 Implement configuration version history
    - Store active/superseded/rolled-back/failed versions with originating session, backend, values, source provenance, and verification
    - Add compare, redacted download, and guarded re-apply APIs and UI
    - _Requirements: 20.1, 20.2_

  - [ ] 25.2 Implement Agent capability diagnostics and setup guide
    - Report connectivity, system info/metrics, pg_stat_statements, query collection, read/write/reload/restart/provider capabilities independently
    - Generate PostgreSQL-version, mode, and backend-specific least-privilege setup instructions
    - _Requirements: 20.3, 20.4_

  - [ ] 25.3 Implement agent lease and duplicate detection
    - Add single-writer lease per host identity
    - Emit duplicate/resolved event codes and block writes while lease ownership is ambiguous
    - _Requirements: 20.5_

  - [ ] 25.4 Implement structured operational event history
    - Add event-code catalog and host_events persistence
    - Add filters for time, severity, code, host, session, component, and text
    - Link events to sessions and configuration versions
    - Emit events for candidate, agent, configuration, approval, reload/restart, rollback, and report outcomes
    - _Requirements: 20.6, 20.7_

  - [ ]* 25.5 Test history, diagnostics, and event safety
    - Property 42: duplicate agents exclude writes
    - Test configuration compare/reapply approval path and redacted download
    - Test event filters and deep links
    - _Requirements: 20.2, 20.5, 20.6, 20.7_

- [ ] 26. Productized tuning checkpoint
  - Run unit, property, integration, real-target, frontend, and browser workflow tests
  - Verify the DBTune-baseline flow from Start tuning through persistent final report
  - Verify both AlterSystemBackend and ManagedConfFileBackend apply/rollback paths
  - Confirm completed sessions remain visible and no normal workflow requires pasted UUIDs

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at logical breakpoints
- Property tests validate universal correctness properties using Hypothesis with 100 iterations per property
- Unit tests validate specific examples and edge cases
- The backend uses Python (FastAPI + asyncpg), the frontend uses React + TypeScript
- Redis Streams provide real-time coordination; PostgreSQL stores platform state
- Docker + docker-compose provide single-command deployment and development setup

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "1.5"] },
    { "id": 3, "tasks": ["2.1", "4.1", "5.1"] },
    { "id": 4, "tasks": ["2.2", "4.2", "5.2"] },
    { "id": 5, "tasks": ["2.3", "2.4", "4.3", "4.4", "4.5", "5.3", "5.4", "5.5"] },
    { "id": 6, "tasks": ["2.5", "5.6"] },
    { "id": 7, "tasks": ["5.7", "5.8", "7.1"] },
    { "id": 8, "tasks": ["7.2", "7.3", "7.4"] },
    { "id": 9, "tasks": ["7.5", "7.6", "7.7", "7.8"] },
    { "id": 10, "tasks": ["7.9", "7.10"] },
    { "id": 11, "tasks": ["7.11", "8.1"] },
    { "id": 12, "tasks": ["8.2", "8.3", "11.1"] },
    { "id": 13, "tasks": ["8.4", "8.5", "8.6", "11.2", "11.3"] },
    { "id": 14, "tasks": ["10.1", "11.4", "12.1"] },
    { "id": 15, "tasks": ["10.2", "10.3", "12.2"] },
    { "id": 16, "tasks": ["12.3", "12.4", "12.5", "12.6"] },
    { "id": 17, "tasks": ["13.1"] },
    { "id": 18, "tasks": ["13.2", "13.3", "13.4", "13.5"] },
    { "id": 19, "tasks": ["15.1"] },
    { "id": 20, "tasks": ["15.2", "15.3", "15.4", "15.5", "15.6"] },
    { "id": 21, "tasks": ["16.1"] },
    { "id": 22, "tasks": ["16.2", "16.3"] },
    { "id": 23, "tasks": ["17.1"] },
    { "id": 24, "tasks": ["17.2", "17.3", "17.4", "17.5", "17.6"] },
    { "id": 25, "tasks": ["19.1", "19.2"] },
    { "id": 26, "tasks": ["19.3", "19.4"] },
    { "id": 27, "tasks": ["20.1", "20.2", "20.3", "20.4"] },
    { "id": 28, "tasks": ["22.1", "23.1", "24.1", "25.1"] },
    { "id": 29, "tasks": ["22.2", "23.2", "24.2", "24.3", "25.2", "25.3"] },
    { "id": 30, "tasks": ["22.3", "23.3", "23.4", "24.4", "24.5", "25.4"] },
    { "id": 31, "tasks": ["22.4", "23.5", "24.6", "25.5"] },
    { "id": 32, "tasks": ["26"] }
  ]
}
```
