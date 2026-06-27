/**
 * API client with typed request/response handlers.
 */

import type {
  HostSummary,
  FleetListResponse,
  RunSummary,
  RunListResponse,
  EvidenceSnapshot,
  EvidenceListResponse,
  PlanDetail,
  PlanListResponse,
  AuditEntry,
  AuditListResponse,
  DBAReport,
  ReportSearchResponse,
  RollbackResponse,
  RollbackStatusResponse,
  DemoStatus,
  PaginatedResponse,
  StartRunRequest,
  ReportSearchQuery,
} from './types';

const BASE_URL = '/api/v1';

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new ApiError(response.status, errorText || response.statusText);
  }

  return response.json();
}

// Fleet API
export const fleetApi = {
  async listHosts(): Promise<HostSummary[]> {
    const response = await request<FleetListResponse | HostSummary[]>('/fleet/');
    return Array.isArray(response) ? response : response.hosts ?? [];
  },
  getHost(hostId: string): Promise<HostSummary> {
    return request<HostSummary>(`/fleet/${hostId}`);
  },
};

// Runs API
export const runsApi = {
  async listActiveRuns(): Promise<RunSummary[]> {
    const response = await request<RunListResponse | RunSummary[]>('/runs/');
    return Array.isArray(response) ? response : response.runs ?? [];
  },
  getRunStatus(runId: string): Promise<RunSummary> {
    return request<RunSummary>(`/runs/${runId}`);
  },
  startRun(data: StartRunRequest): Promise<RunSummary> {
    return request<RunSummary>('/runs/', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },
  haltRun(runId: string): Promise<{ message: string }> {
    return request<{ message: string }>(`/runs/${runId}/halt`, {
      method: 'POST',
    });
  },
};

// Evidence API
export const evidenceApi = {
  async listEvidence(runId: string): Promise<EvidenceSnapshot[]> {
    const response = await request<EvidenceListResponse | EvidenceSnapshot[]>(
      `/evidence/${runId}`,
    );
    return Array.isArray(response) ? response : response.snapshots ?? [];
  },
  getSnapshot(snapshotId: string): Promise<EvidenceSnapshot> {
    return request<EvidenceSnapshot>(`/evidence/snapshot/${snapshotId}`);
  },
};

// Plans API
export const plansApi = {
  async listPendingPlans(page = 1, pageSize = 50): Promise<PaginatedResponse<PlanDetail>> {
    const response = await request<PlanListResponse | PaginatedResponse<PlanDetail>>(
      `/plans/?page=${page}&page_size=${pageSize}`,
    );
    if ('items' in response) {
      return response;
    }
    const total = response.total ?? 0;
    const currentPage = response.page ?? page;
    const currentPageSize = response.page_size ?? pageSize;
    return {
      items: response.plans ?? [],
      total,
      page: currentPage,
      page_size: currentPageSize,
      total_pages: Math.max(1, Math.ceil(total / currentPageSize)),
    };
  },
  getPlan(planId: string): Promise<PlanDetail> {
    return request<PlanDetail>(`/plans/${planId}`);
  },
  approvePlan(planId: string): Promise<{ message: string; status: string }> {
    return request<{ message: string; status: string }>(`/plans/${planId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ approved_by: 'demo_dba@example.com' }),
    });
  },
  rejectPlan(planId: string, reason: string): Promise<{ message: string }> {
    return request<{ message: string }>(`/plans/${planId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ rejected_by: 'demo_dba@example.com', reason }),
    });
  },
};

// Rollback API
export const rollbackApi = {
  initiateRollback(planId: string): Promise<RollbackResponse> {
    return request<RollbackResponse>(`/rollback/${planId}`, {
      method: 'POST',
    });
  },
  getRollbackStatus(planId: string): Promise<RollbackStatusResponse> {
    return request<RollbackStatusResponse>(`/rollback/${planId}/status`);
  },
};

// Audit API
export const auditApi = {
  async getAuditLog(runId: string): Promise<AuditEntry[]> {
    const response = await request<AuditListResponse | AuditEntry[]>(`/audit/${runId}`);
    return Array.isArray(response) ? response : response.entries ?? [];
  },
};

// Reports API
export const reportsApi = {
  getReport(runId: string): Promise<DBAReport> {
    return request<DBAReport>(`/reports/${runId}`);
  },
  async searchReports(query: ReportSearchQuery): Promise<DBAReport[]> {
    const params = new URLSearchParams();
    if (query.date_from) params.set('start_date', query.date_from);
    if (query.date_to) params.set('end_date', query.date_to);
    if (query.host_id) params.set('host_id', query.host_id);
    if (query.keywords) params.set('keywords', query.keywords);
    const response = await request<ReportSearchResponse | DBAReport[]>(
      `/reports/search?${params.toString()}`,
    );
    if (Array.isArray(response)) {
      return response;
    }
    return Promise.all((response.reports ?? []).map((report) => this.getReport(report.run_id)));
  },
};

// Demo API
export const demoApi = {
  activate(): Promise<{ message: string }> {
    return request<{ message: string }>('/demo/activate', { method: 'POST' });
  },
  getStatus(): Promise<DemoStatus> {
    return request<DemoStatus>('/demo/status');
  },
};

export { ApiError };
