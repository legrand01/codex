/**
 * TypeScript types matching the backend Pydantic models.
 */

// Enums
export type HealthStatus = 'healthy' | 'unhealthy' | 'unknown';
export type ConnectionStatus = 'connected' | 'degraded' | 'disconnected';
export type WorkflowStep =
  | 'observe'
  | 'snapshot'
  | 'diagnose'
  | 'propose_plan'
  | 'safety_check'
  | 'approval_gate'
  | 'dry_run'
  | 'apply'
  | 'verify'
  | 'measure'
  | 'keep_rollback'
  | 'report';

export type PlanStatus =
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'pending_forwarding'
  | 'forwarding_failed'
  | 'dry_run_passed'
  | 'dry_run_failed'
  | 'applied'
  | 'rolled_back'
  | 'rollback_failed'
  | 'blocked';

export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'completed'
  | 'failed'
  | 'manually_halted'
  | 'unresponsive'
  | 'timed_out';

export type TuningTarget =
  | 'recommended_fingerprint'
  | 'custom_fingerprint'
  | 'system_wide_aqr'
  | 'transactions_per_second'
  | 'composite';

export type TuningMode = 'reload_only' | 'restart_enabled';

export type ActorType = 'human' | 'system';
export type AuditResult = 'success' | 'failure' | 'blocked';
export type RollbackStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

// Data models
export interface HostSummary {
  id: string;
  hostname: string;
  database_name: string | null;
  health_status: HealthStatus;
  connection_status: ConnectionStatus;
  pg_version: string | null;
  server_role: string | null;
  last_heartbeat: string | null;
}

export interface FleetListResponse {
  hosts: HostSummary[];
  total: number;
}

export interface RunSummary {
  id: string;
  host_id: string | null;
  hostname: string | null;
  database_name: string | null;
  goal: string;
  current_step: WorkflowStep;
  status: RunStatus;
  tuning_target: TuningTarget;
  tuning_mode: TuningMode;
  baseline_score: number | null;
  best_score: number | null;
  current_iteration: number;
  started_at: string;
  completed_at: string | null;
  last_step_transition_at: string;
  elapsed_seconds: number;
}

export interface RunDetail extends RunSummary {
  workload_fingerprint_id: string | null;
  selected_parameters: string[];
  approval_policy: 'per_candidate' | 'final_only';
  warmup_window_seconds: number;
  measurement_window_seconds: number;
  objective_guardrails: Record<string, number>;
  configuration_backend: string;
  max_iterations: number;
  failure_reason: string | null;
  guardrail_violation: Record<string, unknown> | null;
}

export interface RunListResponse {
  runs: RunSummary[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface RunFilters {
  page?: number;
  page_size?: number;
  active_only?: boolean;
  host_id?: string;
  database?: string;
  status?: RunStatus[];
  tuning_target?: TuningTarget;
  tuning_mode?: TuningMode;
  objective?: string;
  date_from?: string;
  date_to?: string;
}

export interface CapabilityCheck {
  key: string;
  label: string;
  status: 'passed' | 'warning' | 'blocked';
  blocking: boolean;
  message: string;
}

export interface ParameterCapability {
  name: string;
  context: 'reload' | 'restart';
  allowlisted: boolean;
  available: boolean;
  reason: string;
}

export interface TuningPreflight {
  host_id: string;
  hostname: string;
  database_name: string | null;
  environment: string;
  platform_type: string;
  configuration_backend: string;
  pg_version: string | null;
  server_role: string | null;
  requested_mode: TuningMode;
  ready: boolean;
  blockers: string[];
  warnings: string[];
  checks: CapabilityCheck[];
  supported_targets: TuningTarget[];
  supported_modes: Array<{ mode: TuningMode; available: boolean; reason: string }>;
  parameters: ParameterCapability[];
  capability_observed_at: string | null;
}

export interface FingerprintCandidate {
  query_id: string;
  query_text: string | null;
  calls: number;
  average_query_runtime_ms: number;
  total_runtime_ms: number;
  runtime_coverage_pct: number;
  impact_score: number;
  recommended: boolean;
  selected: boolean;
  last_seen_at: string | null;
}

export interface FingerprintDiagnostics {
  host_id: string;
  database_name: string | null;
  status: string;
  ready: boolean;
  candidates: FingerprintCandidate[];
  selected_query_ids: string[];
  coverage_pct: number;
  membership_stability_pct: number | null;
  runtime_variance_pct: number | null;
  source_snapshot_id: string | null;
  source_collected_at: string | null;
  snapshot_count: number;
  collector_truncated: boolean;
  warnings: string[];
}

export interface FingerprintMember {
  query_id: string;
  query_text: string | null;
  calls: number;
  average_query_runtime_ms: number;
  total_runtime_ms: number;
  runtime_coverage_pct: number;
  impact_score: number;
  last_seen_at: string | null;
  ordinal: number;
}

export interface WorkloadFingerprint {
  id: string;
  host_id: string;
  database_name: string | null;
  name: string;
  kind: 'recommended' | 'custom';
  status: string;
  ready: boolean;
  selection_criteria: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
  observed_coverage_pct: number;
  membership_stability_pct: number | null;
  runtime_variance_pct: number | null;
  source_snapshot_id: string | null;
  source_collected_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  members: FingerprintMember[];
}

export interface BaselineMeasurement {
  id: string;
  run_id: string;
  host_id: string;
  workload_fingerprint_id: string | null;
  status: 'ready' | 'paused' | 'advisory_only';
  objective_type: TuningTarget;
  objective_formula: string;
  objective_direction: 'minimize' | 'maximize';
  objective_score: number | null;
  metric_units: Record<string, string>;
  fingerprint_membership: Array<Record<string, unknown>>;
  warmup_window_seconds: number;
  requested_measurement_window_seconds: number;
  observed_measurement_window_seconds: number;
  workload_coverage_pct: number;
  runtime_variance_pct: number | null;
  safety_metrics: Record<string, unknown>;
  evidence_references: Array<Record<string, unknown>>;
  root_cause_category: string;
  root_cause_confidence: number;
  root_cause_summary: string;
  root_cause_details: Record<string, unknown>;
  warnings: string[];
  captured_at: string;
}

export interface AdvisoryFinding {
  id: string;
  run_id: string;
  host_id: string;
  category: string;
  severity: 'info' | 'warning' | 'critical';
  title: string;
  summary: string;
  recommendations: string[];
  evidence_references: Array<Record<string, unknown>>;
  executable: false;
  created_at: string;
}

export interface EvidenceSnapshot {
  id: string;
  run_id: string;
  host_id: string;
  evidence_type: string;
  collected_at: string;
  data: Record<string, unknown>;
  quality_score: number | null;
}

export interface EvidenceListResponse {
  run_id: string;
  snapshots: EvidenceSnapshot[];
  categories: Record<string, unknown>[];
  total: number;
}

export interface EvidenceCategory {
  type: string;
  label: string;
  count: number;
}

export interface PlanDetail {
  id: string;
  run_id: string;
  host_id: string;
  status: PlanStatus;
  proposed_changes: Record<string, unknown>[];
  evidence_references: Record<string, unknown>[];
  risk_score: number;
  confidence_score: number;
  uncertainty_explanation: string | null;
  rollback_instructions: Record<string, unknown>[];
  submission_time: string;
}

export interface PlanListResponse {
  plans: PlanDetail[];
  total: number;
  page: number;
  page_size: number;
}

export interface RiskScore {
  score: number;
  breakdown: Record<string, unknown>[];
  host_role_multiplier: number;
  blocked: boolean;
  block_reason: string | null;
}

export interface AuditEntry {
  id: number;
  run_id: string | null;
  timestamp: string;
  actor_type: ActorType;
  actor_name: string;
  action_type: string;
  target_host_id: string | null;
  result: AuditResult;
  result_reason: string | null;
  details: Record<string, unknown> | null;
}

export interface AuditListResponse {
  entries: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface DBAReport {
  id: string;
  run_id: string;
  goal: string;
  outcome_status: string;
  evidence_summaries: Record<string, unknown>[];
  plans_proposed: Record<string, unknown>[];
  approval_decisions: Record<string, unknown>[];
  applied_changes: Record<string, unknown>[];
  verification_results: Record<string, unknown>[];
  generated_at: string;
}

export interface ReportSearchResponse {
  reports: Array<{
    id: string;
    run_id: string;
    goal: string;
    host_id: string | null;
    outcome_status: string;
    generated_at: string;
    expires_at: string | null;
  }>;
  total: number;
}

export interface RollbackResponse {
  plan_id: string;
  status: RollbackStatus;
  message: string;
}

export interface RollbackStatusResponse {
  plan_id: string;
  plan_status: PlanStatus;
  rollback_status: RollbackStatus | 'not_applicable';
  applied_at: string | null;
  rolled_back_at: string | null;
}

export interface DemoStatus {
  active: boolean;
  activated_at: string | null;
}

// Paginated response
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// WebSocket message types
export interface WSFleetUpdate {
  type: 'host_update';
  host: HostSummary;
}

export interface WSRunUpdate {
  type: 'step_transition' | 'status_change' | 'guardrail_violation';
  run_id: string;
  data: Record<string, unknown>;
}

// Request types
export interface StartRunRequest {
  goal: string;
  host_id?: string;
  database_name?: string;
  tuning_target?: TuningTarget;
  tuning_mode?: TuningMode;
  workload_fingerprint_id?: string;
  selected_parameters?: string[];
  approval_policy?: 'per_candidate' | 'final_only';
  warmup_window_seconds?: number;
  measurement_window_seconds?: number;
  objective_guardrails?: Record<string, number>;
  max_iterations?: number;
  max_steps?: number;
}

export interface StartRunResponse {
  run_id: string;
  status: RunStatus;
  goal: string;
  message: string;
}

export interface ReportSearchQuery {
  date_from?: string;
  date_to?: string;
  host_id?: string;
  keywords?: string;
}
