# Requirements Document

## Introduction

The Autonomous Postgres DBA Agent Platform enables database administrators to run guarded, autonomous PostgreSQL investigation and tuning loops from a web-based control plane. The platform follows a structured workflow: observe → snapshot → diagnose → propose plan → safety check → approval gate → dry-run → apply → verify → measure → keep/rollback → report. Every action is auditable, every change is rollback-aware, and human approval is required before any write operation reaches a production database.

## Glossary

- **Control_Plane**: The web application that provides fleet overview, loop monitoring, evidence viewing, plan approval, and rollback controls for database administrators
- **Host_Agent**: A lightweight service deployed on or near a PostgreSQL host that collects telemetry, configuration, and performance evidence
- **AI_Planning_Module**: The component that consumes collected evidence and produces diagnostic recommendations with rollback-aware execution plans
- **Guardrail_Engine**: The safety subsystem that enforces allowlists, risk scoring, evidence-quality thresholds, dry-run verification, and human approval gates before any database modification
- **DBA_Loop_Worker**: An in-app worker that accepts a high-level DBA goal, decomposes it into iterative observe/diagnose/plan/verify steps, and produces a final report
- **Evidence**: Raw telemetry and configuration snapshots collected by the Host_Agent including pg_settings, pg_stat_database, pg_stat_statements samples, lock information, replication lag, WAL/checkpoint signals, and OS-level metrics
- **Plan**: A structured set of proposed PostgreSQL configuration changes or investigative actions produced by the AI_Planning_Module, including risk assessment and rollback instructions
- **Approval_Gate**: A human-in-the-loop checkpoint where a DBA must explicitly approve or reject a proposed Plan before execution
- **Risk_Score**: A numeric assessment of the blast radius and potential impact of a proposed Plan, calculated by the Guardrail_Engine
- **Evidence_Quality_Threshold**: A minimum confidence level that collected Evidence must meet before the AI_Planning_Module produces actionable recommendations
- **DBA_Report**: A final summary document produced at the end of a loop run, containing all decisions, evidence references, outcomes, and audit trail entries
- **Demo_Mode**: A runtime configuration that seeds realistic PostgreSQL fleet data and enables full platform functionality without production database credentials
- **Audit_Log**: A persistent, append-only record of every decision, command, approval, and outcome within the platform
- **Tuning_Session**: A persistent, user-visible optimization run that owns its baseline, workload objective, candidate configurations, Plans, Evidence, approvals, verification results, configuration versions, and final report
- **Workload_Fingerprint**: A named, stable set of normalized PostgreSQL statements used as a repeatable tuning objective, with coverage measured from average query runtime, call count, and total execution time
- **Configuration_Backend**: A target-specific adapter that applies and rolls back settings using parameter-scoped ALTER SYSTEM, a DBTune-owned configuration file, or a managed-cloud provider API
- **Managed_Configuration_File**: A DBTune-owned include file, normally `conf.d/99-dbtune-managed.conf`, whose exact contents and provenance are versioned for atomic apply and rollback
- **Tuning_Candidate**: One bounded configuration evaluated against the same Tuning_Session baseline and objective, with warm-up, measurement, score, guardrail result, and keep/rollback decision

## Requirements

### Requirement 1: Fleet Overview Display

**User Story:** As a DBA, I want to see an overview of all managed PostgreSQL hosts, so that I can quickly assess the health of the fleet.

#### Acceptance Criteria

1. WHEN the DBA navigates to the fleet overview page, THE Control_Plane SHALL display all registered PostgreSQL hosts, each showing: hostname, health status (one of: healthy, unhealthy, or unknown), Host_Agent connection status, PostgreSQL version, and server role
2. THE Control_Plane SHALL display the Host_Agent connection status for each registered host as one of: connected (heartbeat received within the last 60 seconds), degraded (heartbeat received within the last 60–300 seconds), or disconnected (no heartbeat received for more than 300 seconds)
3. WHEN a host health metric crosses a configured threshold, THE Control_Plane SHALL visually distinguish the affected host as unhealthy within 30 seconds of receiving the updated metric
4. THE Control_Plane SHALL display the PostgreSQL version and role (primary or replica) for each registered host
5. IF no hosts are registered, THEN THE Control_Plane SHALL display an empty-state message indicating that no PostgreSQL hosts are registered

### Requirement 2: Active Loop Run Monitoring

**User Story:** As a DBA, I want to monitor all active autonomous DBA loop runs, so that I can track their progress and intervene if needed.

#### Acceptance Criteria

1. THE Control_Plane SHALL display all active DBA_Loop_Worker runs showing for each run: the run identifier, the associated DBA goal, the current workflow step (one of: observe, snapshot, diagnose, propose plan, safety check, approval gate, dry-run, apply, verify, measure, keep/rollback, report), the elapsed time since run start, and the timestamp of the last step transition
2. WHEN a DBA_Loop_Worker transitions between workflow steps, THE Control_Plane SHALL update the displayed step within 5 seconds of the transition occurring
3. WHEN a DBA_Loop_Worker is stopped by a guardrail failure, THE Control_Plane SHALL display the specific guardrail rule that was violated, the workflow step at which the violation occurred, and the timestamp of the stop event
4. WHEN the DBA issues a halt command for an active DBA_Loop_Worker run, THE Control_Plane SHALL stop the run within 10 seconds, transition the run status to "manually halted", and preserve the state of all completed workflow steps up to the point of halting
5. IF a DBA_Loop_Worker becomes unresponsive (no step transition or heartbeat received within 60 seconds), THEN THE Control_Plane SHALL indicate the run as "unresponsive" in the active runs display
6. WHEN the DBA requests a halt on a run that has already completed or been stopped, THE Control_Plane SHALL display a message indicating that the run is no longer active and cannot be halted

### Requirement 3: Evidence Viewer

**User Story:** As a DBA, I want to view the evidence collected by the host agent, so that I can understand what data supports each AI recommendation.

#### Acceptance Criteria

1. WHEN the DBA selects a loop run, THE Control_Plane SHALL display all Evidence snapshots collected during that run, each showing its collection timestamp and Evidence type
2. THE Control_Plane SHALL present Evidence categorized by type: configuration (pg_settings), performance (pg_stat_database, pg_stat_statements), locks, replication, WAL/checkpoint, and OS metrics, with each category displaying the count of snapshots it contains
3. WHEN a Plan references specific Evidence, THE Control_Plane SHALL provide a navigable link from the Plan to the referenced Evidence snapshot such that activating the link scrolls or navigates the view to display the referenced snapshot
4. THE Control_Plane SHALL display Evidence freshness age relative to the current time, expressed in seconds for ages under 60 seconds, in minutes for ages under 60 minutes, and in hours otherwise, updated at least every 30 seconds
5. IF the DBA selects a loop run that has no collected Evidence, THEN THE Control_Plane SHALL display an empty-state message indicating that no Evidence has been collected yet for the selected run
6. IF a Plan references an Evidence snapshot that is unavailable or cannot be retrieved, THEN THE Control_Plane SHALL display the link in a visually distinct disabled state and indicate that the referenced Evidence is unavailable

### Requirement 4: Plan Review and Approval Queue

**User Story:** As a DBA, I want to review AI-generated plans and approve or reject them, so that no automated change reaches my databases without my explicit consent.

#### Acceptance Criteria

1. THE Control_Plane SHALL display a queue of all Plans awaiting human approval, ordered by submission time, paginated with no more than 50 Plans per page
2. WHEN the DBA selects a Plan from the approval queue, THE Control_Plane SHALL display the proposed changes, supporting Evidence references, Risk_Score, uncertainty explanations, and rollback instructions within 3 seconds of selection
3. WHEN the DBA approves a Plan, THE Control_Plane SHALL forward the Plan to the Guardrail_Engine for dry-run execution and record the approval action with timestamp and DBA identity in the Audit_Log
4. IF the Guardrail_Engine is unreachable or does not acknowledge receipt within 30 seconds after the DBA approves a Plan, THEN THE Control_Plane SHALL retain the Plan in a "pending-forwarding" state, display an error indication to the DBA, and retry forwarding up to 3 times at 10-second intervals before marking the Plan as "forwarding-failed"
5. WHEN the DBA rejects a Plan, THE Control_Plane SHALL require the DBA to provide a rejection reason of at least 10 characters, record the rejection reason and DBA identity in the Audit_Log, and notify the DBA_Loop_Worker to re-plan with the rejection reason as feedback
6. THE Control_Plane SHALL prevent any Plan from proceeding to the Guardrail_Engine or to database execution without an explicit DBA approval recorded in the Audit_Log

### Requirement 5: Rollback Controls

**User Story:** As a DBA, I want to rollback any applied change, so that I can recover from unexpected outcomes.

#### Acceptance Criteria

1. WHEN the DBA initiates a rollback for an applied Plan, THE Control_Plane SHALL execute the rollback instructions stored with the original Plan and complete or fail within 300 seconds
2. WHILE a rollback is executing, THE Control_Plane SHALL display the rollback status as one of: pending, in-progress, completed, or failed, updating the displayed status within 5 seconds of any state transition
3. IF a rollback execution fails, THEN THE Control_Plane SHALL alert the DBA with the failure details including the step that failed and the error returned, preserve the Audit_Log entry, and mark the Plan as eligible for rollback retry
4. THE Control_Plane SHALL allow rollback initiation only for Plans whose current status is "applied" or "rollback-failed", and SHALL prevent rollback for Plans whose status is "rolled-back"
5. IF a Plan's stored rollback instructions are missing or cannot be parsed, THEN THE Control_Plane SHALL reject the rollback request, alert the DBA with an error indicating invalid rollback instructions, and log the rejection in the Audit_Log
6. WHEN a rollback completes successfully, THE Control_Plane SHALL transition the Plan status to "rolled-back" and record the rollback outcome in the Audit_Log

### Requirement 6: Host Agent Evidence Collection

**User Story:** As a DBA, I want the host agent to collect comprehensive PostgreSQL telemetry, so that the AI planning module has sufficient data for accurate diagnosis.

#### Acceptance Criteria

1. THE Host_Agent SHALL collect pg_settings configuration snapshots at a configurable interval with a default of 60 seconds and a permitted range of 10 seconds to 3600 seconds
2. THE Host_Agent SHALL collect pg_stat_database and pg_stat_statements query samples at a configurable interval with a default of 30 seconds and a permitted range of 5 seconds to 600 seconds, capturing at most 100 normalized query entries per collection cycle
3. THE Host_Agent SHALL collect current lock information, replication lag, and WAL/checkpoint metrics (checkpoint frequency, WAL generation rate, and last checkpoint age) at a configurable interval with a default of 15 seconds and a permitted range of 5 seconds to 300 seconds
4. THE Host_Agent SHALL collect host OS metrics including CPU utilization percentage, memory usage percentage, and disk I/O operations per second at a configurable interval with a default of 15 seconds and a permitted range of 5 seconds to 300 seconds
5. WHEN the Host_Agent starts up or detects a server role change, THE Host_Agent SHALL report the PostgreSQL version string and current server role (primary or replica) to the Control_Plane within 10 seconds of the triggering event
6. IF the Host_Agent loses connectivity to the Control_Plane, THEN THE Host_Agent SHALL buffer collected Evidence locally up to a maximum of 512 MB and transmit all buffered data to the Control_Plane within 30 seconds of reconnection, in chronological order
7. THE Host_Agent SHALL include a collection timestamp (UTC) and the host identifier with every Evidence snapshot transmitted to the Control_Plane
8. IF an individual Evidence collection query fails, THEN THE Host_Agent SHALL log the failure, skip the failed collection for that cycle, and continue collecting other Evidence types without interruption
9. IF the local Evidence buffer reaches its maximum capacity while disconnected, THEN THE Host_Agent SHALL discard the oldest buffered Evidence to make room for new collections

### Requirement 7: AI Diagnostic and Plan Generation

**User Story:** As a DBA, I want the AI module to produce evidence-based recommendations, so that I can trust the analysis is grounded in real data.

#### Acceptance Criteria

1. THE AI_Planning_Module SHALL produce recommendations that reference only Evidence collected by the Host_Agent during the current loop run
2. THE AI_Planning_Module SHALL never include metric values that are not present in or mathematically derivable solely from the collected Evidence
3. WHEN the collected Evidence does not meet the Evidence_Quality_Threshold for a given recommendation, THE AI_Planning_Module SHALL mark that recommendation as inconclusive, list the specific Evidence types that are missing or insufficient, and omit actionable changes for that recommendation from the Plan
4. THE AI_Planning_Module SHALL include for each recommendation a confidence score between 0.0 and 1.0 and a list of specific Evidence gaps that reduce confidence below 1.0
5. THE AI_Planning_Module SHALL generate a rollback-aware Plan that includes for every proposed change a corresponding reversal action executable by the Control_Plane without additional DBA input
6. THE AI_Planning_Module SHALL include Evidence references (snapshot identifiers and timestamps) for each recommendation in the generated Plan
7. IF the collected Evidence set is empty or all collected Evidence falls below the Evidence_Quality_Threshold, THEN THE AI_Planning_Module SHALL produce no actionable recommendations and SHALL return a Plan containing only a diagnostic summary indicating the insufficient Evidence types needed for analysis

### Requirement 8: Guardrail Allowlist Enforcement

**User Story:** As a DBA, I want the guardrail engine to restrict changes to an allowlist of safe PostgreSQL settings, so that dangerous or unknown parameters cannot be modified.

#### Acceptance Criteria

1. THE Guardrail_Engine SHALL maintain a configurable allowlist of PostgreSQL settings that may be modified, and SHALL reject all proposed setting modifications when the allowlist is empty
2. WHEN a Plan proposes modification of one or more settings not on the allowlist, THE Guardrail_Engine SHALL reject the entire Plan and record the violation in the Audit_Log including the disallowed setting name(s) and the target host identifier
3. THE Guardrail_Engine SHALL classify each allowlisted setting as reload-safe or restart-required based on the PostgreSQL parameter context, and SHALL permit only reload-safe parameter changes by default
4. IF a Plan proposes a restart-required parameter change and the DBA has not enabled restart-required changes for the target host, THEN THE Guardrail_Engine SHALL reject the Plan and record the violation in the Audit_Log
5. WHEN the DBA enables restart-required changes for a target host, THE Guardrail_Engine SHALL apply that enablement to the specified host until the DBA explicitly revokes it or the current loop run completes

### Requirement 9: Guardrail Safety Checks

**User Story:** As a DBA, I want comprehensive safety checks before any change is applied, so that risk is minimized and I retain full control.

#### Acceptance Criteria

1. THE Guardrail_Engine SHALL calculate a Risk_Score in the range 0 to 100 for each Plan, where the score increases with the number of affected settings, the percentage deviation of each proposed value from the current value, and the host role weight (primary hosts weighted higher than replicas)
2. WHEN a Plan Risk_Score exceeds a configurable threshold (default: 70), THE Guardrail_Engine SHALL block execution, record the block decision in the Audit_Log, and notify the DBA through the Control_Plane
3. THE Guardrail_Engine SHALL execute a dry-run of the Plan on the target host before applying changes, verifying that proposed SQL statements parse correctly and target settings present in the host's pg_settings, within a configurable timeout (default: 30 seconds)
4. THE Guardrail_Engine SHALL require a rollback plan to be present and valid before permitting Plan execution, where valid means the rollback plan contains a restore value for every setting modified by the Plan and each restore value matches the pre-change snapshot value
5. THE Guardrail_Engine SHALL require explicit human approval through the Approval_Gate before executing any write operation on a database host
6. IF a dry-run produces an error or exceeds the configured timeout, THEN THE Guardrail_Engine SHALL block Plan execution, record the failure in the Audit_Log, and report the dry-run error or timeout condition to the DBA
7. THE Guardrail_Engine SHALL enforce the safety workflow ordering: risk scoring and allowlist checks first, then Approval_Gate, then dry-run, then apply — and SHALL not proceed to a later stage if any earlier stage fails

### Requirement 10: Audit Logging and Secrets Redaction

**User Story:** As a DBA, I want a complete audit trail of all platform actions with secrets redacted, so that I can review historical decisions and maintain compliance.

#### Acceptance Criteria

1. THE Guardrail_Engine SHALL record every decision, command execution, approval, rejection, and outcome in the Audit_Log within 5 seconds of the event occurring
2. THE Audit_Log SHALL be append-only such that no existing entry can be updated or deleted through platform interfaces, and any attempt to modify a historical entry SHALL be rejected
3. WHEN writing to the Audit_Log, THE Guardrail_Engine SHALL redact passwords, connection strings, API keys, tokens, and certificate values by replacing detected secret content with a fixed placeholder while preserving the surrounding log structure
4. THE Audit_Log SHALL include for each entry: an ISO 8601 timestamp, actor identification (specifying whether human or system component and the actor name), action type, target host identifier, and result indicating success, failure, or blocked with a reason
5. WHEN the DBA requests audit history for a specific loop run, THE Control_Plane SHALL display all Audit_Log entries associated with that run in chronological order within 10 seconds of the request
6. IF the Guardrail_Engine detects that a value matching a known secret pattern could not be redacted due to a processing error, THEN THE Guardrail_Engine SHALL block the Audit_Log write, log a redaction-failure alert, and retry redaction before persisting the entry
7. THE DBA_Loop_Worker SHALL record every decision point, command issued, approval outcome, and intermediate result in the Audit_Log using the same entry structure defined in criterion 4

### Requirement 11: Autonomous DBA Loop Execution

**User Story:** As a DBA, I want to start an autonomous tuning loop by providing a high-level goal, so that the system investigates and proposes solutions iteratively without constant manual intervention.

#### Acceptance Criteria

1. WHEN the DBA submits a high-level goal, THE DBA_Loop_Worker SHALL decompose the goal into a sequence of observe/diagnose/plan/verify steps not exceeding a configurable maximum of 20 steps
2. THE DBA_Loop_Worker SHALL execute iterative loops up to a configurable maximum of 10 iterations, collecting new Evidence from the Host_Agent at each observation step before proceeding to diagnosis
3. WHEN the DBA_Loop_Worker produces a Plan requiring database modification, THE DBA_Loop_Worker SHALL submit the Plan to the Guardrail_Engine and pause execution until the Approval_Gate is resolved or a configurable approval timeout with a default of 24 hours elapses
4. IF the Guardrail_Engine rejects a Plan or a guardrail check fails, THEN THE DBA_Loop_Worker SHALL stop execution and record the failure reason in the Audit_Log
5. THE DBA_Loop_Worker SHALL record every decision point, command issued, and intermediate result in the Audit_Log throughout execution
6. WHEN the DBA_Loop_Worker completes all steps or is halted, THE DBA_Loop_Worker SHALL generate a DBA_Report summarizing goals, evidence collected, plans proposed, actions taken, outcomes measured, and any unresolved issues
7. IF the approval timeout elapses without a resolution, THEN THE DBA_Loop_Worker SHALL halt execution and record the timeout in the Audit_Log
8. IF Evidence collection fails during an observation step, THEN THE DBA_Loop_Worker SHALL retry collection once after 10 seconds and halt execution with a failure recorded in the Audit_Log if the retry also fails
9. IF the DBA_Loop_Worker reaches the configured maximum iteration count without achieving the goal, THEN THE DBA_Loop_Worker SHALL halt execution and generate a DBA_Report indicating the goal was not achieved within the iteration limit

### Requirement 12: Post-Apply Verification and Rollback Decision

**User Story:** As a DBA, I want the system to verify the impact of applied changes and automatically rollback if verification fails, so that applied changes are confirmed beneficial.

#### Acceptance Criteria

1. WHEN a Plan is applied successfully, THE DBA_Loop_Worker SHALL collect verification Evidence from the target host within a configurable observation window (minimum 10 seconds, maximum 600 seconds, default 60 seconds)
2. WHEN verification Evidence collection completes, THE DBA_Loop_Worker SHALL compare pre-apply and post-apply Evidence for the same metric categories collected in the observation step (pg_stat_database, pg_stat_statements, lock information, replication lag, WAL/checkpoint signals, and OS metrics) and record the per-metric delta
3. IF any monitored metric degrades beyond a configurable threshold percentage relative to its pre-apply baseline (default: 10% degradation), THEN THE DBA_Loop_Worker SHALL initiate rollback of the applied Plan and record the triggering metric, measured delta, and threshold in the Audit_Log
4. WHEN all monitored metrics remain within the configured degradation threshold relative to pre-apply baselines, THE DBA_Loop_Worker SHALL mark the change as kept and proceed to the next step
5. IF the DBA_Loop_Worker fails to collect verification Evidence within the configured observation window (due to host unavailability or collection error), THEN THE DBA_Loop_Worker SHALL initiate rollback of the applied Plan and record the collection failure reason in the Audit_Log

### Requirement 13: DBA Report Generation

**User Story:** As a DBA, I want a comprehensive final report for each loop run, so that I have a permanent record of what was investigated, what was changed, and what was the outcome.

#### Acceptance Criteria

1. WHEN a DBA_Loop_Worker run completes (whether successfully, partially, or due to failure), THE DBA_Loop_Worker SHALL generate a DBA_Report within 30 seconds containing: the original goal, all Evidence summaries with their confidence scores, all Plans proposed, approval decisions, applied changes, verification results, and final outcome status (success, partial success, or failure)
2. THE DBA_Report SHALL label each item with a classification of either "AI_RECOMMENDATION" (for AI-generated suggestions not yet validated by measurement) or "VERIFIED_FACT" (for outcomes confirmed by post-change Evidence collection), so that a reader can determine the provenance of every statement without ambiguity
3. THE DBA_Report SHALL identify any recommendations where supporting Evidence scored below the configured confidence threshold, marking them as "INCONCLUSIVE" with a reference to the specific Evidence gap
4. THE Control_Plane SHALL make DBA_Reports retrievable and searchable by date range, host identifier, and goal keywords, returning matching results within 5 seconds
5. IF DBA_Report generation fails, THEN THE DBA_Loop_Worker SHALL log the failure with the run identifier and persist the raw run data so that the report can be regenerated on retry
6. THE Control_Plane SHALL retain DBA_Reports for a minimum of 90 days from the date of generation

### Requirement 14: Demo Mode Operation

**User Story:** As a new user, I want to explore the full platform with realistic sample data, so that I can evaluate its capabilities without connecting production databases.

#### Acceptance Criteria

1. WHEN Demo_Mode is enabled, THE Control_Plane SHALL seed the fleet overview with at least 3 PostgreSQL hosts representing each Host_Agent connection status (connected, disconnected, degraded) and at least one host in each health state (healthy, unhealthy)
2. WHEN Demo_Mode is enabled, THE Host_Agent SHALL generate synthetic Evidence containing at least one sample for each of the following categories: slow query samples, configuration drift scenarios, replication lag events, checkpoint pressure signals, and weak-evidence cases that do not meet the Evidence_Quality_Threshold
3. WHEN Demo_Mode is enabled, THE DBA_Loop_Worker SHALL execute loops against synthetic data and produce at least one successful loop outcome and at least one blocked or inconclusive loop outcome demonstrating Guardrail_Engine enforcement
4. WHILE Demo_Mode is active, THE Control_Plane SHALL reject any connection attempts to real database hosts and SHALL NOT transmit network requests to any host address not designated as synthetic
5. THE Control_Plane SHALL display a persistent visual indicator identifying Demo_Mode on every page, visible without scrolling, that remains present for the entire duration of the Demo_Mode session
6. WHEN Demo_Mode is enabled, THE DBA_Loop_Worker SHALL generate at least one Plan that requires Approval_Gate interaction, allowing the user to exercise the approve and reject workflows with synthetic data

### Requirement 15: Deployment and Development Setup

**User Story:** As a developer, I want clear deployment and local development instructions, so that I can run, test, and deploy the platform reliably.

#### Acceptance Criteria

1. WHEN the provided Dockerfile is built, THE Control_Plane SHALL produce a container image that starts without error and passes a health-check endpoint responding with HTTP 200 within 30 seconds of container start
2. WHEN the docker-compose configuration is executed, THE Control_Plane SHALL start all required services (application and database) and reach a ready state where the health-check endpoint returns HTTP 200 within 60 seconds
3. WHEN the local development setup script is executed, THE Control_Plane SHALL install all dependencies, start the application with hot-reload enabled, and confirm readiness by responding to HTTP requests on the configured port within 60 seconds
4. THE Control_Plane SHALL include a README containing at minimum: an architecture overview section, step-by-step setup instructions for local development, a list of all required environment variables with descriptions and example values, and a demo walkthrough section with numbered steps that exercise at least one complete plan-generation-to-execution workflow
5. WHEN the automated test suite is executed, THE Control_Plane SHALL run tests covering guardrail enforcement, loop execution, evidence collection, and plan generation workflows, and the suite SHALL exit with a zero exit code when all tests pass
6. WHEN the deployment script is executed, THE Control_Plane SHALL be accessible via a web browser at a configurable host and port, returning a valid HTTP response within 30 seconds of script completion
7. IF the container build fails or any required service fails to start within the specified timeout, THEN THE Control_Plane SHALL exit with a non-zero exit code and output a message indicating which component failed to initialize

### Requirement 16: Persistent Tuning Session Workspace

**User Story:** As a DBA, I want every tuning session and all of its related information in one persistent workspace, so that I can start, monitor, review, and revisit tuning without copying identifiers between pages.

#### Acceptance Criteria

1. THE Control_Plane SHALL provide a Tuning landing page containing a primary `Start tuning` action and a persistent history of Tuning_Sessions in every status, including queued, running, waiting approval, completed, failed, manually halted, unresponsive, and timed out
2. WHEN a Tuning_Session completes, fails, or is halted, THE Control_Plane SHALL retain it in session history and SHALL NOT remove it from the default Runs or Tuning view merely because it is no longer active
3. WHEN the DBA selects a Tuning_Session, THE Control_Plane SHALL navigate to a stable route containing the run identifier and SHALL automatically scope Overview, Plans, Evidence, Configuration, Activity, Rollback, and Report views to that selected run
4. THE Tuning_Session workspace SHALL present Plans, Evidence, Configuration changes, Activity, and Report as tabs or sections on the same page and SHALL NOT require the DBA to paste a run or plan UUID to move between them
5. THE Tuning_Session header SHALL remain visible across its tabs and display host, database, objective, tuning mode, session status, current workflow step, baseline score, best score, start time, completion time, and safe available actions
6. WHEN a completed Tuning_Session is displayed, elapsed duration SHALL be calculated using its completion timestamp and SHALL remain stable instead of continuing to increase
7. THE Control_Plane SHALL allow session history filtering by host, database, status, tuning target, mode, and date range, and SHALL provide direct links from every history row to the session workspace
8. THE Control_Plane SHALL preserve the selected Tuning_Session while the DBA switches among its tabs, refreshes the page, or follows an Evidence or Plan reference

### Requirement 17: Workload Objectives and Candidate Optimization

**User Story:** As a DBA, I want tuning to optimize a measured workload objective rather than make one-shot guesses from pg_settings, so that each retained configuration is proven against the database's real workload.

#### Acceptance Criteria

1. WHEN the DBA starts tuning, THE Control_Plane SHALL require selection of a target host/database, a reload-only or restart-enabled mode, and one objective: recommended Workload_Fingerprint, custom Workload_Fingerprint, system-wide average query runtime, transactions per second, or a configured composite objective
2. THE Host_Agent SHALL collect normalized query identifiers and, when explicitly enabled, query text together with calls, average query runtime, total execution time, runtime coverage, and last-seen timestamp from pg_stat_statements
3. THE Control_Plane SHALL recommend Workload_Fingerprint members using both average query runtime and call count, SHALL display runtime coverage, and SHALL warn when visible queries represent insufficient or inconsistent workload coverage
4. THE DBA SHALL be able to create a named custom Workload_Fingerprint by selecting one or more normalized statements, and the platform SHALL preserve membership and selection criteria for later Tuning_Sessions
5. BEFORE evaluating a Tuning_Candidate, THE DBA_Loop_Worker SHALL capture a stable baseline observation window for the selected objective and the applicable safety metrics
6. THE optimization loop SHALL evaluate multiple bounded Tuning_Candidates against the same baseline, workload definition, warm-up period, measurement window, and scoring method rather than treating a proposed value as beneficial before measurement
7. AFTER each candidate measurement, THE DBA_Loop_Worker SHALL record the candidate values, objective score, baseline delta, best-so-far delta, safety-metric deltas, workload coverage, and statistical confidence or noise warning
8. THE DBA_Loop_Worker SHALL keep a candidate only when it improves the selected objective without violating configured safety guardrails; otherwise it SHALL restore the best verified configuration or exact baseline
9. IF the workload changes materially, coverage falls below threshold, or measurement variance exceeds the configured noise limit, THEN THE DBA_Loop_Worker SHALL pause candidate evaluation and require a fresh baseline or DBA decision
10. BEFORE proposing configuration changes, THE DBA_Loop_Worker SHALL classify whether the dominant evidence indicates configuration, query plan or missing index, lock contention, vacuum/bloat, storage/CPU saturation, connection pressure, or insufficient evidence; when configuration is not a plausible dominant lever, it SHALL avoid blind parameter changes and SHALL produce a separate advisory diagnosis whose query/index actions remain non-executable until governed by a future allowlisted workflow

### Requirement 18: PostgreSQL Parameter Coverage and Tuning Modes

**User Story:** As a DBA, I want to see every supported PostgreSQL tuning parameter and its disposition, so that I know what was changed, retained, blocked, or not evaluated.

#### Acceptance Criteria

1. THE reload-only mode SHALL support independent allowlisting of: work_mem, random_page_cost, seq_page_cost, checkpoint_completion_target, effective_io_concurrency, max_parallel_workers_per_gather, max_parallel_workers, max_wal_size, min_wal_size, bgwriter_lru_maxpages, bgwriter_delay, effective_cache_size, maintenance_work_mem, default_statistics_target, and max_parallel_maintenance_workers
2. THE restart-enabled mode MAY additionally support shared_buffers, max_worker_processes, wal_buffers, and huge_pages, and SHALL clearly identify that these values cannot become active through pg_reload_conf() alone
3. THE Control_Plane SHALL display every supported parameter for the selected PostgreSQL version with current value, unit, source, source file or provider, context, pending-restart state, allowlist state, baseline value, best verified value, pending candidate value, and final disposition
4. THE final disposition for each supported parameter SHALL be exactly one of: changed and verified, retained at baseline, blocked by policy, restart required, unsupported on target, not applicable to objective, or inconclusive due to insufficient evidence
5. THE DBA SHALL be able to include or exclude individual supported parameters before a Tuning_Session begins, subject to organization and host guardrails
6. THE Control_Plane SHALL provide a configurable human-in-the-loop mode that requires explicit approval before every candidate apply, while retaining mandatory final approval and production write interlocks
7. THE Control_Plane SHALL expose independently configurable regression guardrails for average query runtime, transactions per second, Workload_Fingerprint performance, locks, replication, WAL/checkpoints, CPU, memory, and I/O

### Requirement 19: Pluggable Configuration Apply and Rollback

**User Story:** As a DBA, I want DBTune changes isolated from my hand-managed configuration, so that ownership, verification, and rollback are explicit across self-managed and managed PostgreSQL platforms.

#### Acceptance Criteria

1. THE platform SHALL implement a Configuration_Backend interface with at least `alter_system`, `managed_conf_file`, and provider-managed adapters, and SHALL record the chosen backend with each host and Tuning_Session
2. THE `alter_system` backend SHALL use parameter-scoped ALTER SYSTEM privileges where supported, SHALL verify values after pg_reload_conf(), and SHALL preserve existing postgres.auto.conf provenance during rollback
3. THE `managed_conf_file` backend SHALL be available only for explicitly enrolled self-managed hosts whose Host_Agent has verified filesystem access and a postgresql.conf include or include_dir path that loads the DBTune-owned file
4. BEFORE using `managed_conf_file`, THE Host_Agent SHALL verify include ordering, directory and file ownership, write permissions, configuration-file location, absence of command-line overrides, and absence of conflicting postgres.auto.conf entries for every managed parameter
5. THE DBTune-owned file SHALL use a deterministic late-loading name such as `conf.d/99-dbtune-managed.conf`; the platform SHALL fail closed when another later include, postgres.auto.conf entry, command-line setting, database/user setting, or provider setting would override a proposed value
6. APPLY through `managed_conf_file` SHALL render only allowlisted settings, write a temporary file in the same filesystem, fsync it, validate the resulting configuration through pg_file_settings, atomically rename it, call pg_reload_conf(), and verify both effective value and pg_settings sourcefile
7. THE platform SHALL store a checksum and exact previous bytes of the DBTune-owned file before every apply; rollback SHALL atomically restore those bytes or remove the file when it did not previously exist, reload configuration, and verify both value and provenance
8. IF pg_file_settings reports a syntax, name, value, or precedence error, pg_reload_conf() returns false, or effective values fail to converge, THEN the Configuration_Backend SHALL restore the previous version immediately and mark the operation failed
9. RESTART-context settings SHALL be staged separately, displayed as pending restart, and SHALL never be reported as active until a controlled restart and post-restart verification succeed
10. Managed-cloud targets without filesystem access SHALL use the provider adapter and SHALL NOT require or simulate conf.d file ownership

### Requirement 20: Configuration History, Agent Diagnostics, and Events

**User Story:** As a DBA, I want configuration versions, agent capability health, and operational events visible beside tuning sessions, so that I can understand what is active and why an action is blocked.

#### Acceptance Criteria

1. THE Control_Plane SHALL provide configuration history per host/database, showing each version's identifier, active state, creation and apply timestamps, originating Tuning_Session, backend, parameter values, and verification outcome
2. THE DBA SHALL be able to compare any two configuration versions, download a redacted representation, and request application of an eligible prior version through the same approval and guardrail workflow
3. THE Control_Plane SHALL display Agent capability status separately for connectivity, system information, system metrics, pg_stat_statements, query-text collection, configuration read access, configuration write access, reload permission, restart capability, and provider API capability
4. THE Agent setup view SHALL generate version- and mode-specific least-privilege instructions, including pg_monitor, pg_stat_statements prerequisites, per-parameter ALTER SYSTEM grants, pg_reload_conf permission, or managed-file ownership requirements as applicable
5. THE platform SHALL detect simultaneous active Host_Agents for the same host identity, emit a coded event, block ambiguous write execution, and identify when the duplicate-agent condition is resolved
6. THE Control_Plane SHALL provide filterable Event history by time range, severity, event code, host, session, component, and free-text search, with direct links from events to the affected Tuning_Session or configuration version
7. Agent failures, candidate decisions, approvals, applies, reloads, restarts, rollbacks, precedence conflicts, workload coverage warnings, and report generation outcomes SHALL emit structured event codes in addition to append-only Audit_Log entries
