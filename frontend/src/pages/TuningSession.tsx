import { createContext, useContext, useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  auditApi,
  evidenceApi,
  plansApi,
  reportsApi,
  rollbackApi,
  runsApi,
} from '../api/client';
import type {
  AuditEntry,
  DBAReport,
  EvidenceSnapshot,
  PlanDetail,
  RunDetail,
} from '../api/types';
import { useApi } from '../hooks/useApi';
import { EmptyState, LoadingSpinner, StatusBadge } from '../components';

type Tab = 'overview' | 'plans' | 'configuration' | 'workload' | 'evidence' | 'activity' | 'report';
const tabs: Array<{ id: Tab; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'plans', label: 'Plans' },
  { id: 'configuration', label: 'Configuration' },
  { id: 'workload', label: 'Workload' },
  { id: 'evidence', label: 'Evidence' },
  { id: 'activity', label: 'Activity' },
  { id: 'report', label: 'Report' },
];
const validTabs = new Set(tabs.map((tab) => tab.id));
const card: React.CSSProperties = {
  border: '1px solid #e5e7eb', borderRadius: '8px', padding: '14px', background: '#fff',
};
const subtle: React.CSSProperties = { color: '#6b7280', fontSize: '0.8rem' };
const SessionIdContext = createContext('');

function useSessionId(): string {
  const runId = useContext(SessionIdContext);
  if (!runId) throw new Error('Tuning session route context is missing');
  return runId;
}

function asRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : [];
}

function snapshotRecords(snapshot: EvidenceSnapshot | undefined, keys: string[]): Record<string, unknown>[] {
  if (!snapshot) return [];
  for (const key of keys) {
    const records = asRecords(snapshot.data[key]);
    if (records.length) return records;
  }
  return [];
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function displayValue(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

function JsonItems({ items, empty }: { items: Record<string, unknown>[]; empty: string }) {
  if (!items.length) return <EmptyState title={empty} />;
  return <div style={{ display: 'grid', gap: '8px' }}>{items.map((item, index) => (
    <pre key={index} style={{ ...card, margin: 0, whiteSpace: 'pre-wrap', fontSize: '0.76rem', overflow: 'auto' }}>
      {JSON.stringify(item, null, 2)}
    </pre>
  ))}</div>;
}

interface SessionData {
  run: RunDetail;
  plans: PlanDetail[];
  evidence: EvidenceSnapshot[];
  activity: AuditEntry[];
  report: DBAReport | null;
  refreshPlans: () => void;
  refreshRun: () => void;
}

function OverviewTab({ data }: { data: SessionData }) {
  const { run, plans, evidence, activity, report } = data;
  const stats = [
    ['Current step', run.current_step?.replace(/_/g, ' ') ?? 'Finished'],
    ['Iteration', `${run.current_iteration} of ${run.max_iterations}`],
    ['Duration', formatDuration(run.elapsed_seconds)],
    ['Plans', String(plans.length)],
    ['Evidence', String(evidence.length)],
    ['Activity', String(activity.length)],
  ];
  return <div style={{ display: 'grid', gap: '14px' }}>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '10px' }}>
      {stats.map(([label, value]) => <div key={label} style={card}>
        <div style={{ ...subtle, textTransform: 'uppercase' }}>{label}</div>
        <strong style={{ textTransform: 'capitalize' }}>{value}</strong>
      </div>)}
    </div>
    <div style={{ ...card, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px' }}>
      <div><div style={subtle}>Objective</div><strong>{run.tuning_target.replace(/_/g, ' ')}</strong></div>
      <div><div style={subtle}>Approval policy</div><strong>{run.approval_policy.replace(/_/g, ' ')}</strong></div>
      <div><div style={subtle}>Measurement</div><strong>{run.warmup_window_seconds}s warm-up + {run.measurement_window_seconds}s measured</strong></div>
      <div><div style={subtle}>Configuration backend</div><strong>{run.configuration_backend.replace(/_/g, ' ')}</strong></div>
      <div><div style={subtle}>Baseline score</div><strong>{run.baseline_score ?? 'Not scored'}</strong></div>
      <div><div style={subtle}>Best verified score</div><strong>{run.best_score ?? 'Not scored'}</strong></div>
    </div>
    {run.failure_reason && <div style={{ ...card, borderColor: '#fecaca', background: '#fef2f2', color: '#991b1b' }}>
      <strong>Session stopped:</strong> {run.failure_reason}
    </div>}
    {report && <div style={card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
        <div><div style={subtle}>Measured outcome</div><strong style={{ textTransform: 'capitalize' }}>{report.outcome_status.replace(/_/g, ' ')}</strong></div>
      </div>
      <p style={{ marginBottom: 0 }}>{report.applied_changes.length} applied change(s), {report.verification_results.length} verification result(s).</p>
    </div>}
  </div>;
}

function PlansTab({ data, openEvidence }: { data: SessionData; openEvidence: (id?: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const act = async (plan: PlanDetail, action: 'approve' | 'reject' | 'rollback') => {
    setWorking(plan.id);
    setMessage(null);
    try {
      if (action === 'approve') await plansApi.approvePlan(plan.id);
      if (action === 'reject') {
        if (rejectReason.trim().length < 10) throw new Error('Rejection reason must be at least 10 characters.');
        await plansApi.rejectPlan(plan.id, rejectReason.trim());
      }
      if (action === 'rollback') await rollbackApi.initiateRollback(plan.id);
      setMessage(action === 'approve' ? 'Plan approved and returned to the worker.' : action === 'reject' ? 'Plan rejected.' : 'Rollback initiated.');
      setRejectReason('');
      data.refreshPlans();
      data.refreshRun();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `Unable to ${action} plan`);
    } finally {
      setWorking(null);
    }
  };

  if (!data.plans.length) return <EmptyState title="No plans yet" description="Plans will appear here when the measured baseline produces safe candidates." />;
  return <div style={{ display: 'grid', gap: '10px' }}>
    {message && <div style={{ ...card, background: '#eff6ff', color: '#1e40af' }}>{message}</div>}
    {data.plans.map((plan) => {
      const isOpen = expanded === plan.id;
      const eligibleRollback = ['applied', 'rollback_failed'].includes(plan.status);
      return <div key={plan.id} style={card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'start' }}>
          <div><strong>Plan {plan.id.slice(0, 8)}</strong><div style={subtle}>{new Date(plan.submission_time).toLocaleString()} · {plan.proposed_changes.length} change(s)</div></div>
          <StatusBadge type="plan" status={plan.status} />
        </div>
        <div style={{ display: 'flex', gap: '18px', marginTop: '10px', fontSize: '0.85rem' }}>
          <span>Risk <strong style={{ color: plan.risk_score > 70 ? '#b91c1c' : '#166534' }}>{plan.risk_score}/100</strong></span>
          <span>Confidence <strong>{Math.round(plan.confidence_score * 100)}%</strong></span>
        </div>
        <button onClick={() => setExpanded(isOpen ? null : plan.id)} style={{ marginTop: '10px', border: 0, padding: 0, background: 'transparent', color: '#2563eb', cursor: 'pointer' }}>{isOpen ? 'Hide details' : 'Review plan'}</button>
        {isOpen && <div style={{ borderTop: '1px solid #e5e7eb', marginTop: '12px', paddingTop: '12px' }}>
          {plan.uncertainty_explanation && <p><strong>Uncertainty:</strong> {plan.uncertainty_explanation}</p>}
          <h4>Proposed changes</h4>
          <JsonItems items={plan.proposed_changes} empty="No proposed changes" />
          {plan.evidence_references.length > 0 && <div style={{ marginTop: '12px' }}><strong>Evidence:</strong>{' '}
            {plan.evidence_references.slice(0, 5).map((reference, index) => {
              const id = typeof reference.snapshot_id === 'string' ? reference.snapshot_id : undefined;
              return <button key={id ?? index} onClick={() => openEvidence(id)} style={{ margin: '2px 4px', border: 0, background: '#eff6ff', color: '#1d4ed8', borderRadius: '4px', padding: '3px 6px' }}>{id ? id.slice(0, 8) : `Evidence ${index + 1}`}</button>;
            })}
          </div>}
          {plan.status === 'pending_approval' && <div style={{ marginTop: '14px', paddingTop: '12px', borderTop: '1px solid #e5e7eb' }}>
            <button disabled={working === plan.id} onClick={() => act(plan, 'approve')} style={{ padding: '8px 12px', background: '#15803d', color: '#fff', border: 0, borderRadius: '5px', marginRight: '8px' }}>Approve for dry-run</button>
            <input value={rejectReason} onChange={(event) => setRejectReason(event.target.value)} placeholder="Rejection reason (10+ characters)" style={{ padding: '8px', minWidth: '260px', border: '1px solid #d1d5db', borderRadius: '5px' }} />
            <button disabled={working === plan.id} onClick={() => act(plan, 'reject')} style={{ padding: '8px 12px', background: '#fff', color: '#b91c1c', border: '1px solid #fecaca', borderRadius: '5px', marginLeft: '8px' }}>Reject</button>
          </div>}
          {eligibleRollback && <button disabled={working === plan.id} onClick={() => act(plan, 'rollback')} style={{ marginTop: '14px', padding: '8px 12px', background: '#b91c1c', color: '#fff', border: 0, borderRadius: '5px' }}>Initiate verified rollback</button>}
        </div>}
      </div>;
    })}
  </div>;
}

function ConfigurationTab({ data }: { data: SessionData }) {
  const snapshots = data.evidence.filter((item) => item.evidence_type === 'pg_settings');
  const earliest = snapshotRecords(snapshots[0], ['settings']);
  const latest = snapshotRecords(snapshots[snapshots.length - 1], ['settings']);
  const baseline = new Map(earliest.map((setting) => [String(setting.name), setting]));
  const current = new Map(latest.map((setting) => [String(setting.name), setting]));
  const proposals = new Map<string, { change: Record<string, unknown>; status: string }>();
  for (const plan of data.plans) for (const change of plan.proposed_changes) {
    const name = String(change.setting_name ?? change.name ?? '');
    if (name) proposals.set(name, { change, status: plan.status });
  }
  const names = data.run.selected_parameters.length
    ? data.run.selected_parameters
    : Array.from(new Set([...proposals.keys(), ...baseline.keys()])).slice(0, 20);
  if (!names.length) return <EmptyState title="No configuration scope recorded" />;
  return <div>
    <div style={{ ...card, marginBottom: '12px' }}><strong>{data.run.configuration_backend.replace(/_/g, ' ')}</strong><div style={subtle}>{names.length} session parameter(s) · baseline and latest observed values shown together</div></div>
    <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
      <thead><tr>{['Parameter', 'Baseline', 'Latest observed', 'Candidate', 'Context', 'Source', 'Disposition'].map((label) => <th key={label} style={{ textAlign: 'left', padding: '9px', borderBottom: '1px solid #d1d5db' }}>{label}</th>)}</tr></thead>
      <tbody>{names.map((name) => {
        const before = baseline.get(name) ?? {};
        const now = current.get(name) ?? before;
        const proposal = proposals.get(name);
        const proposedValue = proposal?.change.proposed_value ?? proposal?.change.value;
        const disposition = proposal ? (proposal.status === 'applied' ? 'changed and verified' : proposal.status.replace(/_/g, ' ')) : 'retained at baseline';
        return <tr key={name}><td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}><code>{name}</code></td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(before.setting)} {displayValue(before.unit, '')}</td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(now.setting)} {displayValue(now.unit, '')}</td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(proposedValue)}</td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(now.context)}</td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(now.source)}</td>
          <td style={{ padding: '9px', borderBottom: '1px solid #e5e7eb', textTransform: 'capitalize' }}>{disposition}</td></tr>;
      })}</tbody>
    </table></div>
  </div>;
}

function WorkloadTab({ data }: { data: SessionData }) {
  const statementSnapshots = data.evidence.filter((item) => ['pg_stat_statements', 'pg_stats'].includes(item.evidence_type));
  const latest = statementSnapshots[statementSnapshots.length - 1];
  const statements = snapshotRecords(latest, ['statements', 'statement_stats', 'queries']);
  const sorted = [...statements].sort((left, right) => Number(right.total_exec_time ?? 0) - Number(left.total_exec_time ?? 0));
  const totalTime = sorted.reduce((sum, statement) => sum + Number(statement.total_exec_time ?? 0), 0);
  if (!sorted.length) return <EmptyState title="No workload statements captured" description="The Host Agent must collect pg_stat_statements before workload analysis can begin." />;
  return <div>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '10px', marginBottom: '12px' }}>
      <div style={card}><div style={subtle}>Visible statements</div><strong>{sorted.length}</strong></div>
      <div style={card}><div style={subtle}>Captured execution time</div><strong>{totalTime.toFixed(1)} ms</strong></div>
      <div style={card}><div style={subtle}>Objective</div><strong>{data.run.tuning_target.replace(/_/g, ' ')}</strong></div>
    </div>
    <div style={{ ...card, marginBottom: '12px', background: '#fffbeb', borderColor: '#fde68a' }}>Coverage is limited to statements visible to the agent during this session. Candidate optimization should pause when this sample is unstable or insufficient.</div>
    <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
      <thead><tr>{['Query ID', 'Normalized query', 'Calls', 'Mean runtime', 'Total runtime', 'Visible coverage'].map((label) => <th key={label} style={{ textAlign: 'left', padding: '8px', borderBottom: '1px solid #d1d5db' }}>{label}</th>)}</tr></thead>
      <tbody>{sorted.slice(0, 25).map((statement, index) => {
        const exec = Number(statement.total_exec_time ?? 0);
        return <tr key={String(statement.queryid ?? index)}><td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb' }}><code>{displayValue(statement.queryid).slice(0, 14)}</code></td>
          <td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb', maxWidth: '430px' }}>{displayValue(statement.query).slice(0, 220)}</td>
          <td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb' }}>{displayValue(statement.calls, '0')}</td>
          <td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb' }}>{Number(statement.mean_exec_time ?? 0).toFixed(2)} ms</td>
          <td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb' }}>{exec.toFixed(1)} ms</td>
          <td style={{ padding: '8px', borderBottom: '1px solid #e5e7eb' }}>{totalTime ? `${((exec / totalTime) * 100).toFixed(1)}%` : '—'}</td></tr>;
      })}</tbody>
    </table></div>
  </div>;
}

function EvidenceTab({ data }: { data: SessionData }) {
  const grouped = useMemo(() => {
    const result = new Map<string, EvidenceSnapshot[]>();
    for (const snapshot of data.evidence) {
      const values = result.get(snapshot.evidence_type) ?? [];
      values.push(snapshot);
      result.set(snapshot.evidence_type, values);
    }
    return Array.from(result.entries());
  }, [data.evidence]);
  if (!grouped.length) return <EmptyState title="No evidence yet" description="Baseline snapshots will remain attached to this session." />;
  return <div style={{ display: 'grid', gap: '14px' }}>{grouped.map(([type, snapshots]) => <section key={type}>
    <h3 style={{ margin: '0 0 7px', textTransform: 'capitalize' }}>{type.replace(/_/g, ' ')} <span style={subtle}>({snapshots.length})</span></h3>
    <div style={{ display: 'grid', gap: '6px' }}>{[...snapshots].reverse().map((snapshot) => {
      const payload = JSON.stringify(snapshot.data, null, 2);
      return <details key={snapshot.id} id={`evidence-${snapshot.id}`} style={card}>
        <summary style={{ cursor: 'pointer' }}><strong>{new Date(snapshot.collected_at).toLocaleString()}</strong> <span style={subtle}>· Quality {snapshot.quality_score === null ? 'not scored' : `${Math.round(snapshot.quality_score * 100)}%`} · {snapshot.id.slice(0, 8)}</span></summary>
        <pre style={{ whiteSpace: 'pre-wrap', overflow: 'auto', maxHeight: '360px', fontSize: '0.72rem', background: '#f9fafb', padding: '10px' }}>{payload.length > 8000 ? `${payload.slice(0, 8000)}\n… payload truncated in UI` : payload}</pre>
      </details>;
    })}</div>
  </section>)}</div>;
}

function ActivityTab({ data }: { data: SessionData }) {
  if (!data.activity.length) return <EmptyState title="No activity recorded yet" />;
  return <div style={{ borderLeft: '2px solid #dbeafe', marginLeft: '8px', paddingLeft: '18px' }}>{data.activity.map((entry) => <div key={entry.id} style={{ ...card, marginBottom: '9px', position: 'relative' }}>
    <span style={{ position: 'absolute', width: '10px', height: '10px', borderRadius: '50%', background: entry.result === 'success' ? '#16a34a' : entry.result === 'blocked' ? '#d97706' : '#dc2626', left: '-24px', top: '18px' }} />
    <strong style={{ textTransform: 'capitalize' }}>{entry.action_type.replace(/_/g, ' ')}</strong>
    <div style={subtle}>{new Date(entry.timestamp).toLocaleString()} · {entry.actor_name} · {entry.result}</div>
    {entry.result_reason && <div style={{ marginTop: '5px' }}>{entry.result_reason}</div>}
  </div>)}</div>;
}

function ReportTab({ data, error }: { data: SessionData; error: string | null }) {
  const report = data.report;
  if (!report) return <EmptyState title="Report not generated yet" description={error ? 'The session has not produced a final report, or it is unavailable.' : 'The final measured outcome will appear here when the session completes.'} />;
  const sections: Array<[string, Record<string, unknown>[]]> = [
    ['Evidence summary', report.evidence_summaries],
    ['Plans proposed', report.plans_proposed],
    ['Approval decisions', report.approval_decisions],
    ['Applied changes', report.applied_changes],
    ['Verification results', report.verification_results],
  ];
  return <div style={{ display: 'grid', gap: '14px' }}>
    <div style={{ ...card, display: 'flex', justifyContent: 'space-between', gap: '12px' }}><div><div style={subtle}>Outcome</div><strong style={{ textTransform: 'capitalize' }}>{report.outcome_status.replace(/_/g, ' ')}</strong><p style={{ marginBottom: 0 }}>{report.goal}</p></div><div style={{ ...subtle, textAlign: 'right' }}>Generated<br />{new Date(report.generated_at).toLocaleString()}</div></div>
    {sections.map(([label, items]) => <section key={label}><h3>{label} <span style={subtle}>({items.length})</span></h3><JsonItems items={items} empty={`No ${label.toLowerCase()}`} /></section>)}
  </div>;
}

function SessionWorkspace() {
  const runId = useSessionId();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get('tab') as Tab | null;
  const tab: Tab = requestedTab && validTabs.has(requestedTab) ? requestedTab : 'overview';
  const runRequest = useApi<RunDetail>(() => runsApi.getRunStatus(runId), [runId]);
  const plansRequest = useApi<PlanDetail[]>(() => plansApi.listRunPlans(runId), [runId]);
  const evidenceRequest = useApi<EvidenceSnapshot[]>(() => evidenceApi.listEvidence(runId), [runId]);
  const activityRequest = useApi<AuditEntry[]>(() => auditApi.getAuditLog(runId), [runId]);
  const reportRequest = useApi<DBAReport>(() => reportsApi.getReport(runId), [runId]);

  const selectTab = (next: Tab) => setSearchParams(next === 'overview' ? {} : { tab: next });
  const openEvidence = (snapshotId?: string) => {
    setSearchParams({ tab: 'evidence' });
    if (snapshotId) window.setTimeout(() => document.getElementById(`evidence-${snapshotId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 0);
  };

  if (runRequest.loading) return <LoadingSpinner message="Loading tuning session..." />;
  if (runRequest.error || !runRequest.data) return <div style={{ color: '#b91c1c' }}>Unable to load session: {runRequest.error}</div>;
  const run = runRequest.data;
  const data: SessionData = {
    run,
    plans: plansRequest.data ?? [],
    evidence: evidenceRequest.data ?? [],
    activity: activityRequest.data ?? [],
    report: reportRequest.data,
    refreshPlans: plansRequest.refetch,
    refreshRun: runRequest.refetch,
  };
  const secondaryLoading = plansRequest.loading || evidenceRequest.loading || activityRequest.loading;

  return <div>
    <Link to="/runs" style={{ color: '#2563eb', fontSize: '0.85rem' }}>← Tuning sessions</Link>
    <header style={{ ...card, margin: '12px 0 14px', padding: '18px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'start' }}>
        <div><h2 style={{ margin: '0 0 5px' }}>{run.goal}</h2><code style={{ ...subtle, fontSize: '0.73rem' }}>{run.id}</code></div>
        <StatusBadge type="run" status={run.status} />
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 18px', marginTop: '14px', fontSize: '0.82rem' }}>
        <span><strong>Host:</strong> {run.hostname ?? run.host_id ?? 'Unknown'}</span>
        <span><strong>Database:</strong> {run.database_name ?? 'Not recorded'}</span>
        <span><strong>Mode:</strong> {run.tuning_mode.replace(/_/g, ' ')}</span>
        <span><strong>Objective:</strong> {run.tuning_target.replace(/_/g, ' ')}</span>
        <span><strong>Started:</strong> {new Date(run.started_at).toLocaleString()}</span>
        {run.completed_at && <span><strong>Completed:</strong> {new Date(run.completed_at).toLocaleString()}</span>}
      </div>
    </header>
    <nav aria-label="Tuning session sections" style={{ display: 'flex', gap: '3px', overflowX: 'auto', borderBottom: '1px solid #d1d5db', marginBottom: '18px' }}>
      {tabs.map((item) => <button key={item.id} onClick={() => selectTab(item.id)} aria-current={tab === item.id ? 'page' : undefined} style={{ padding: '10px 13px', border: 0, borderBottom: tab === item.id ? '3px solid #2563eb' : '3px solid transparent', background: 'transparent', color: tab === item.id ? '#1d4ed8' : '#4b5563', fontWeight: tab === item.id ? 600 : 400, cursor: 'pointer', whiteSpace: 'nowrap' }}>{item.label}{item.id === 'plans' && data.plans.length ? ` (${data.plans.length})` : ''}{item.id === 'evidence' && data.evidence.length ? ` (${data.evidence.length})` : ''}</button>)}
    </nav>
    {secondaryLoading && tab !== 'overview' ? <LoadingSpinner message={`Loading ${tab}...`} /> : <>
      {tab === 'overview' && <OverviewTab data={data} />}
      {tab === 'plans' && <PlansTab data={data} openEvidence={openEvidence} />}
      {tab === 'configuration' && <ConfigurationTab data={data} />}
      {tab === 'workload' && <WorkloadTab data={data} />}
      {tab === 'evidence' && <EvidenceTab data={data} />}
      {tab === 'activity' && <ActivityTab data={data} />}
      {tab === 'report' && <ReportTab data={data} error={reportRequest.error} />}
    </>}
  </div>;
}

export function TuningSession() {
  const { runId = '' } = useParams();
  if (!runId) return <div style={{ color: '#b91c1c' }}>A tuning session ID is required.</div>;
  return <SessionIdContext.Provider value={runId}><SessionWorkspace /></SessionIdContext.Provider>;
}
