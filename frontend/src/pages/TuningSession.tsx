import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { auditApi, evidenceApi, plansApi, reportsApi, runsApi } from '../api/client';
import type { AuditEntry, DBAReport, EvidenceSnapshot, PlanDetail, RunSummary } from '../api/types';
import { useApi } from '../hooks/useApi';
import { EmptyState, LoadingSpinner, StatusBadge } from '../components';

type Tab = 'overview' | 'plans' | 'configuration' | 'evidence' | 'activity' | 'report';
const tabs: Array<{ id: Tab; label: string }> = [
  { id: 'overview', label: 'Overview' }, { id: 'plans', label: 'Plans' },
  { id: 'configuration', label: 'Configuration' }, { id: 'evidence', label: 'Evidence' },
  { id: 'activity', label: 'Activity' }, { id: 'report', label: 'Report' },
];
const card: React.CSSProperties = { border: '1px solid #e5e7eb', borderRadius: '8px', padding: '14px', marginBottom: '10px', background: '#fff' };

function JsonList({ items, empty }: { items: Record<string, unknown>[]; empty: string }) {
  if (!items.length) return <EmptyState title={empty} />;
  return <div>{items.map((item, index) => <pre key={index} style={{ ...card, whiteSpace: 'pre-wrap', fontSize: '0.78rem', overflow: 'auto' }}>{JSON.stringify(item, null, 2)}</pre>)}</div>;
}

export function TuningSession() {
  const { runId = '' } = useParams();
  const [tab, setTab] = useState<Tab>('overview');
  const run = useApi<RunSummary>(() => runsApi.getRunStatus(runId), [runId]);
  const plans = useApi<PlanDetail[]>(() => plansApi.listRunPlans(runId), [runId]);
  const evidence = useApi<EvidenceSnapshot[]>(() => evidenceApi.listEvidence(runId), [runId]);
  const activity = useApi<AuditEntry[]>(() => auditApi.getAuditLog(runId), [runId]);
  const report = useApi<DBAReport>(() => reportsApi.getReport(runId), [runId]);

  if (run.loading) return <LoadingSpinner message="Loading tuning session..." />;
  if (run.error || !run.data) return <div style={{ color: '#b91c1c' }}>Unable to load session: {run.error}</div>;
  const session = run.data;
  const proposed = (plans.data ?? []).flatMap((plan) => plan.proposed_changes);

  return <div>
    <Link to="/runs" style={{ color: '#2563eb', fontSize: '0.85rem' }}>← Tuning sessions</Link>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', margin: '12px 0 18px' }}>
      <div><h2 style={{ margin: '0 0 4px' }}>{session.goal}</h2><code style={{ color: '#6b7280', fontSize: '0.75rem' }}>{session.id}</code></div>
      <StatusBadge type="run" status={session.status} />
    </div>
    <div style={{ display: 'flex', gap: '4px', overflowX: 'auto', borderBottom: '1px solid #d1d5db', marginBottom: '20px' }}>
      {tabs.map((item) => <button key={item.id} onClick={() => setTab(item.id)} style={{ padding: '10px 14px', border: 0, borderBottom: tab === item.id ? '3px solid #2563eb' : '3px solid transparent', background: 'transparent', color: tab === item.id ? '#1d4ed8' : '#4b5563', fontWeight: tab === item.id ? 600 : 400, cursor: 'pointer' }}>{item.label}</button>)}
    </div>

    {tab === 'overview' && <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: '12px' }}>
      {[['Status', session.status], ['Current step', session.current_step?.replace(/_/g, ' ') ?? 'Finished'], ['Iteration', `${session.current_iteration}`], ['Duration', `${Math.round(session.elapsed_seconds)} seconds`], ['Plans', `${plans.data?.length ?? 0}`], ['Evidence', `${evidence.data?.length ?? 0}`]].map(([label, value]) => <div key={label} style={card}><div style={{ color: '#6b7280', fontSize: '0.75rem', textTransform: 'uppercase' }}>{label}</div><strong style={{ textTransform: 'capitalize' }}>{value}</strong></div>)}
    </div>}

    {tab === 'plans' && (plans.loading ? <LoadingSpinner /> : !(plans.data?.length) ? <EmptyState title="No plans yet" description="Plans will appear here when the baseline produces safe candidates." /> : <div>{plans.data.map((plan) => <div key={plan.id} style={card}><div style={{ display: 'flex', justifyContent: 'space-between' }}><strong>Plan {plan.id.slice(0, 8)}</strong><StatusBadge type="plan" status={plan.status} /></div><p style={{ marginBottom: 0 }}>{plan.proposed_changes.length} proposed change(s) · Risk {plan.risk_score} · Confidence {Math.round(plan.confidence_score * 100)}%</p></div>)}</div>)}
    {tab === 'configuration' && <JsonList items={proposed} empty="No configuration changes proposed yet" />}
    {tab === 'evidence' && (evidence.loading ? <LoadingSpinner /> : !(evidence.data?.length) ? <EmptyState title="No evidence yet" description="Baseline snapshots will remain attached to this session." /> : <div>{evidence.data.map((item) => <div key={item.id} style={card}><strong>{item.evidence_type.replace(/_/g, ' ')}</strong><div style={{ color: '#6b7280', fontSize: '0.8rem' }}>{new Date(item.collected_at).toLocaleString()} · Quality {item.quality_score === null ? 'not scored' : `${Math.round(item.quality_score * 100)}%`}</div></div>)}</div>)}
    {tab === 'activity' && (activity.loading ? <LoadingSpinner /> : !(activity.data?.length) ? <EmptyState title="No activity recorded yet" /> : <div>{activity.data.map((entry) => <div key={entry.id} style={card}><strong>{entry.action_type.replace(/_/g, ' ')}</strong><div style={{ color: '#6b7280', fontSize: '0.8rem' }}>{new Date(entry.timestamp).toLocaleString()} · {entry.actor_name} · {entry.result}</div>{entry.result_reason && <div>{entry.result_reason}</div>}</div>)}</div>)}
    {tab === 'report' && (report.loading ? <LoadingSpinner /> : report.error || !report.data ? <EmptyState title="Report not generated yet" description="The final measured outcome will appear here when the session completes." /> : <div><div style={card}><strong>{report.data.outcome_status.replace(/_/g, ' ')}</strong><p>{report.data.goal}</p></div><JsonList items={report.data.verification_results} empty="No verification results" /></div>)}
  </div>;
}
