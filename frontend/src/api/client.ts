/**
 * API client with typed request/response handlers.
 */

import type {
  HostSummary,
  FleetListResponse,
  RunSummary,
  RunDetail,
  RunListResponse,
  RunFilters,
  TuningMode,
  TuningPreflight,
  FingerprintDiagnostics,
  WorkloadFingerprint,
  BaselineMeasurement,
  AdvisoryFinding,
  TuningCandidate,
  ParameterDisposition,
  ConfigurationVersion,
  EvidenceSnapshot,
  EvidenceSnapshotSummary,
  EvidenceListResponse,
  EvidenceLifecycleStatus,
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
  StartRunResponse,
  ReportSearchQuery,
  ConfigurationCompare,
  OperationalEvent,
  CapabilityDiagnostic,
  SetupGuide,
} from './types';

const BASE_URL = '/api/v1';
const TOKEN_KEY = 'dbtune_api_token';

export function getApiToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) ?? '';
}

export function setApiToken(token: string): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

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
  const token = getApiToken();
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new ApiError(response.status, errorText || response.statusText);
  }

  return response.json();
}

async function requestText(path: string): Promise<string> {
  const token = getApiToken();
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new ApiError(response.status, await response.text());
  return response.text();
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
  getDiagnostics(hostId: string): Promise<CapabilityDiagnostic> {
    return request<CapabilityDiagnostic>(`/fleet/${hostId}/diagnostics`);
  },
  getSetup(hostId: string, mode = 'reload_only'): Promise<SetupGuide> {
    return request<SetupGuide>(`/fleet/${hostId}/setup?mode=${encodeURIComponent(mode)}`);
  },
};

// Runs API
export const runsApi = {
  async listRunHistory(filters: RunFilters = {}): Promise<RunListResponse> {
    const params = new URLSearchParams();
    if (filters.page) params.set('page', String(filters.page));
    if (filters.page_size) params.set('page_size', String(filters.page_size));
    if (filters.active_only) params.set('active_only', 'true');
    if (filters.host_id) params.set('host_id', filters.host_id);
    if (filters.database) params.set('database', filters.database);
    for (const status of filters.status ?? []) params.append('status', status);
    if (filters.tuning_target) params.set('tuning_target', filters.tuning_target);
    if (filters.tuning_mode) params.set('tuning_mode', filters.tuning_mode);
    if (filters.objective) params.set('objective', filters.objective);
    if (filters.date_from) params.set('date_from', filters.date_from);
    if (filters.date_to) params.set('date_to', filters.date_to);
    const response = await request<RunListResponse | RunSummary[]>(
      `/runs/?${params.toString()}`,
    );
    return Array.isArray(response)
      ? { runs: response, total: response.length, page: 1, page_size: response.length, total_pages: 1 }
      : response;
  },
  async listRuns(filters: RunFilters = {}): Promise<RunSummary[]> {
    return (await this.listRunHistory(filters)).runs;
  },
  getRunStatus(runId: string): Promise<RunDetail> {
    return request<RunDetail>(`/runs/${runId}`);
  },
  startRun(data: StartRunRequest): Promise<StartRunResponse> {
    return request<StartRunResponse>('/runs/', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },
  getPreflight(hostId: string, mode: TuningMode): Promise<TuningPreflight> {
    const params = new URLSearchParams({ host_id: hostId, mode });
    return request<TuningPreflight>(`/runs/preflight?${params.toString()}`);
  },
  haltRun(runId: string): Promise<{ message: string }> {
    return request<{ message: string }>(`/runs/${runId}/halt`, {
      method: 'POST',
    });
  },
};

// Workload fingerprint API
export const fingerprintsApi = {
  getCandidates(hostId: string, databaseName?: string): Promise<FingerprintDiagnostics> {
    const params = new URLSearchParams({ host_id: hostId });
    if (databaseName) params.set('database_name', databaseName);
    return request<FingerprintDiagnostics>(`/fingerprints/candidates?${params.toString()}`);
  },
  async list(hostId?: string, databaseName?: string): Promise<WorkloadFingerprint[]> {
    const params = new URLSearchParams();
    if (hostId) params.set('host_id', hostId);
    if (databaseName) params.set('database_name', databaseName);
    const response = await request<{ fingerprints: WorkloadFingerprint[] }>(
      `/fingerprints/?${params.toString()}`,
    );
    return response.fingerprints ?? [];
  },
  get(fingerprintId: string): Promise<WorkloadFingerprint> {
    return request<WorkloadFingerprint>(`/fingerprints/${fingerprintId}`);
  },
  recommend(data: {
    host_id: string;
    database_name?: string;
    name?: string;
    include_query_text?: boolean;
  }): Promise<WorkloadFingerprint> {
    return request<WorkloadFingerprint>('/fingerprints/recommend', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },
  create(data: {
    host_id: string;
    database_name?: string;
    name: string;
    query_ids: string[];
    include_query_text?: boolean;
  }): Promise<WorkloadFingerprint> {
    return request<WorkloadFingerprint>('/fingerprints/', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },
};

// Baseline measurement and advisory API
export const baselinesApi = {
  get(runId: string): Promise<BaselineMeasurement> {
    return request<BaselineMeasurement>(`/runs/${runId}/baseline`);
  },
  listAdvisories(runId: string): Promise<AdvisoryFinding[]> {
    return request<AdvisoryFinding[]>(`/runs/${runId}/advisories`);
  },
};

export const candidatesApi = {
  list(runId: string): Promise<TuningCandidate[]> {
    return request<TuningCandidate[]>(`/runs/${runId}/candidates`);
  },
};

export const parameterCatalogApi = {
  listDispositions(runId: string): Promise<ParameterDisposition[]> {
    return request<ParameterDisposition[]>(`/runs/${runId}/parameter-dispositions`);
  },
  listConfigurationVersions(runId: string): Promise<ConfigurationVersion[]> {
    return request<ConfigurationVersion[]>(`/runs/${runId}/configuration-versions`);
  },
};

export const configurationsApi = {
  compare(leftId: string, rightId: string): Promise<ConfigurationCompare> {
    return request<ConfigurationCompare>(
      `/configurations/compare?left_id=${leftId}&right_id=${rightId}`,
    );
  },
  download(versionId: string): Promise<string> {
    return requestText(`/configurations/${versionId}/download`);
  },
  reapply(versionId: string): Promise<{
    run_id: string; plan_id: string; status: string; run_href: string;
  }> {
    return request(`/configurations/${versionId}/reapply`, { method: 'POST' });
  },
};

export const eventsApi = {
  async list(filters: Record<string, string> = {}): Promise<OperationalEvent[]> {
    const params = new URLSearchParams(filters);
    const response = await request<{ events: OperationalEvent[] }>(
      `/events/?${params.toString()}`,
    );
    return response.events ?? [];
  },
};

// Evidence API
export const evidenceApi = {
  getLifecycleStatus(): Promise<EvidenceLifecycleStatus> {
    return request<EvidenceLifecycleStatus>('/evidence/lifecycle/status');
  },
  listEvidencePage(
    runId: string,
    options: { evidenceType?: string; limit?: number; offset?: number } = {},
  ): Promise<EvidenceListResponse> {
    const params = new URLSearchParams();
    if (options.evidenceType) params.set('evidence_type', options.evidenceType);
    if (options.limit !== undefined) params.set('limit', String(options.limit));
    if (options.offset !== undefined) params.set('offset', String(options.offset));
    const suffix = params.toString();
    return request<EvidenceListResponse>(`/evidence/${runId}${suffix ? `?${suffix}` : ''}`);
  },
  async listEvidence(runId: string): Promise<EvidenceSnapshotSummary[]> {
    return (await this.listEvidencePage(runId)).snapshots;
  },
  getSnapshot(snapshotId: string): Promise<EvidenceSnapshot> {
    return request<EvidenceSnapshot>(`/evidence/snapshot/${snapshotId}`);
  },
  async getLatestSnapshot(runId: string, evidenceType: string): Promise<EvidenceSnapshot | null> {
    const page = await this.listEvidencePage(runId, { evidenceType, limit: 1 });
    return page.snapshots.length ? this.getSnapshot(page.snapshots[0].id) : null;
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
  async listRunPlans(runId: string): Promise<PlanDetail[]> {
    const response = await request<PlanListResponse>(
      `/plans/?run_id=${encodeURIComponent(runId)}&pending_only=false`,
    );
    return response.plans ?? [];
  },
  getPlan(planId: string): Promise<PlanDetail> {
    return request<PlanDetail>(`/plans/${planId}`);
  },
  approvePlan(planId: string): Promise<{ message: string; status: string }> {
    return request<{ message: string; status: string }>(`/plans/${planId}/approve`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  },
  rejectPlan(planId: string, reason: string): Promise<{ message: string }> {
    return request<{ message: string }>(`/plans/${planId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
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
