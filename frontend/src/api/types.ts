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
  | 'running'
  | 'completed'
  | 'failed'
  | 'manually_halted'
  | 'unresponsive'
  | 'timed_out';

export type ActorType = 'human' | 'system';
export type AuditResult = 'success' | 'failure' | 'blocked';
export type RollbackStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

// Data models
export interface HostSummary {
  id: string;
  hostname: string;
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
  goal: string;
  current_step: WorkflowStep;
  status: RunStatus;
  current_iteration: number;
  started_at: string;
  last_step_transition_at: string;
  elapsed_seconds: number;
}

export interface RunListResponse {
  runs: RunSummary[];
  total: number;
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
  max_iterations?: number;
  max_steps?: number;
}

export interface ReportSearchQuery {
  date_from?: string;
  date_to?: string;
  host_id?: string;
  keywords?: string;
}
