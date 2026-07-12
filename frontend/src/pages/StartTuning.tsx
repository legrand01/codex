import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { fleetApi, runsApi } from '../api/client';
import type { HostSummary } from '../api/types';
import { useApi } from '../hooks/useApi';
import { LoadingSpinner } from '../components';

export function StartTuning() {
  const navigate = useNavigate();
  const { data: hosts, loading, error } = useApi<HostSummary[]>(() => fleetApi.listHosts(), []);
  const [hostId, setHostId] = useState('');
  const [goal, setGoal] = useState('Improve PostgreSQL performance safely and report measurable results');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!goal.trim()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await runsApi.startRun({ goal: goal.trim(), host_id: hostId || undefined });
      navigate(`/tuning/${result.run_id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Failed to start tuning');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <LoadingSpinner message="Loading database targets..." />;

  return <div style={{ maxWidth: '720px', margin: '0 auto' }}>
    <Link to="/runs" style={{ color: '#2563eb', fontSize: '0.85rem' }}>← Tuning sessions</Link>
    <h2 style={{ marginBottom: '4px' }}>Start tuning</h2>
    <p style={{ marginTop: 0, color: '#6b7280' }}>The agent will collect a baseline before proposing any change. Applying a plan still requires approval.</p>
    <form onSubmit={submit} style={{ border: '1px solid #e5e7eb', borderRadius: '10px', padding: '20px' }}>
      <label style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Database target</label>
      <select value={hostId} onChange={(event) => setHostId(event.target.value)} style={{ width: '100%', padding: '10px', marginBottom: '18px', border: '1px solid #d1d5db', borderRadius: '6px' }}>
        <option value="">Use the first registered target</option>
        {(hosts ?? []).map((host) => <option key={host.id} value={host.id}>{host.hostname} · {host.connection_status}</option>)}
      </select>
      <label style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Tuning goal</label>
      <textarea value={goal} onChange={(event) => setGoal(event.target.value)} rows={4} style={{ width: '100%', boxSizing: 'border-box', padding: '10px', border: '1px solid #d1d5db', borderRadius: '6px', resize: 'vertical' }} />
      {(error || submitError) && <div style={{ color: '#b91c1c', marginTop: '12px' }}>{submitError || error}</div>}
      <button type="submit" disabled={submitting || !goal.trim()} style={{ marginTop: '18px', padding: '10px 16px', background: '#2563eb', color: '#fff', border: 0, borderRadius: '6px', fontWeight: 600, cursor: 'pointer' }}>
        {submitting ? 'Starting…' : 'Start tuning'}
      </button>
    </form>
  </div>;
}
