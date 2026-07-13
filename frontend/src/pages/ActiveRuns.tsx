import { useState, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { fleetApi, runsApi } from '../api/client';
import type { HostSummary, RunFilters, RunListResponse, RunStatus, RunSummary, TuningMode, TuningTarget, WSRunUpdate } from '../api/types';
import { useApi } from '../hooks/useApi';
import { useWebSocket } from '../hooks/useWebSocket';
import { StatusBadge, DataTable, EmptyState, LoadingSpinner } from '../components';
import type { Column } from '../components';

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function isUnresponsive(run: RunSummary): boolean {
  if (run.status !== 'running') return false;
  const lastTransition = new Date(run.last_step_transition_at).getTime();
  const now = Date.now();
  return (now - lastTransition) > 60000;
}

export function ActiveRuns() {
  const [filters, setFilters] = useState<RunFilters>({ page: 1, page_size: 25 });
  const [draftObjective, setDraftObjective] = useState('');
  const [draftDatabase, setDraftDatabase] = useState('');
  const { data: history, loading, error, refetch } = useApi<RunListResponse>(
    () => runsApi.listRunHistory(filters),
    [JSON.stringify(filters)],
  );
  const { data: hosts } = useApi<HostSummary[]>(() => fleetApi.listHosts(), []);
  const [runList, setRunList] = useState<RunSummary[] | null>(null);
  const [haltingId, setHaltingId] = useState<string | null>(null);
  const [haltConfirmId, setHaltConfirmId] = useState<string | null>(null);
  const [haltError, setHaltError] = useState<string | null>(null);

  useEffect(() => setRunList(history?.runs ?? null), [history]);

  const handleWSMessage = useCallback((msg: unknown) => {
    const update = msg as WSRunUpdate;
    if (update.run_id) {
      setRunList((prev) => {
        const current = prev || history?.runs || [];
        return current.map((r) =>
          r.id === update.run_id
            ? { ...r, ...(update.data as Partial<RunSummary>) }
            : r,
        );
      });
    }
  }, [history]);

  useWebSocket({
    url: '/ws/fleet',
    onMessage: handleWSMessage,
  });

  const handleHalt = async (runId: string) => {
    setHaltingId(runId);
    setHaltError(null);
    try {
      const result = await runsApi.haltRun(runId);
      setHaltConfirmId(null);
      // Update local state
      setRunList((prev) => {
        const current = prev || history?.runs || [];
        return current.map((r) =>
          r.id === runId ? { ...r, status: 'manually_halted' as const } : r,
        );
      });
      if (result.message) {
        setHaltError(result.message);
      }
    } catch (err) {
      setHaltError(err instanceof Error ? err.message : 'Failed to halt run');
    } finally {
      setHaltingId(null);
    }
  };

  const displayRuns = runList || history?.runs;
  const displayedCount = displayRuns?.length ?? 0;
  const hasActiveFilters = Object.entries(filters).some(
    ([key, value]) => !['page', 'page_size'].includes(key) && value !== undefined && value !== '',
  );

  const setFilter = <K extends keyof RunFilters>(key: K, value: RunFilters[K]) => {
    setFilters((current) => ({ ...current, [key]: value, page: 1 }));
  };

  const applyTextFilters = (event: React.FormEvent) => {
    event.preventDefault();
    setFilters((current) => ({
      ...current,
      page: 1,
      objective: draftObjective.trim() || undefined,
      database: draftDatabase.trim() || undefined,
    }));
  };

  const clearFilters = () => {
    setDraftObjective('');
    setDraftDatabase('');
    setFilters({ page: 1, page_size: 25 });
  };

  const columns: Column<RunSummary>[] = [
    {
      key: 'id',
      header: 'Run ID',
      render: (r) => <code style={{ fontSize: '0.75rem' }}>{r.id.slice(0, 8)}...</code>,
    },
    {
      key: 'goal',
      header: 'Goal',
      render: (r) => (
        <span style={{ maxWidth: '250px', display: 'inline-block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {r.goal}
        </span>
      ),
    },
    {
      key: 'current_step',
      header: 'Current Step',
      render: (r) => (
        <span style={{ textTransform: 'capitalize' }}>
          {r.current_step.replace(/_/g, ' ')}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (r) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <StatusBadge type="run" status={r.status} />
          {isUnresponsive(r) && (
            <span style={{
              fontSize: '0.7rem',
              color: '#dc2626',
              fontWeight: 600,
              padding: '2px 6px',
              backgroundColor: '#fef2f2',
              borderRadius: '4px',
            }}>
              UNRESPONSIVE
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'elapsed',
      header: 'Elapsed',
      render: (r) => formatElapsed(r.elapsed_seconds),
    },
    {
      key: 'last_transition',
      header: 'Last Transition',
      render: (r) => new Date(r.last_step_transition_at).toLocaleTimeString(),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (r) => {
        const active = ['queued', 'running', 'waiting_approval', 'unresponsive'].includes(r.status);
        if (haltConfirmId === r.id) {
          return (
            <div style={{ display: 'flex', gap: '4px' }}>
              <button
                onClick={() => handleHalt(r.id)}
                disabled={haltingId === r.id}
                style={{
                  padding: '4px 8px',
                  fontSize: '0.75rem',
                  backgroundColor: '#dc2626',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer',
                }}
              >
                {haltingId === r.id ? 'Halting...' : 'Confirm'}
              </button>
              <button
                onClick={() => setHaltConfirmId(null)}
                style={{
                  padding: '4px 8px',
                  fontSize: '0.75rem',
                  backgroundColor: '#e5e7eb',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>
            </div>
          );
        }
        return <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Link to={`/tuning/${r.id}`} style={{ fontSize: '0.8rem', color: '#2563eb' }}>View</Link>
          {active && <button
              onClick={() => setHaltConfirmId(r.id)}
              style={{ padding: '4px 8px', fontSize: '0.75rem', backgroundColor: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: '4px', cursor: 'pointer' }}
            >Halt</button>}
        </div>;
      },
    },
  ];

  if (loading) return <LoadingSpinner message="Loading tuning sessions..." />;
  if (error) return <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error} <button onClick={refetch}>Retry</button></div>;
  if ((!displayRuns || displayRuns.length === 0) && !hasActiveFilters) {
    return (
      <EmptyState
        title="No Tuning Sessions"
        description="Start tuning to collect a baseline and create the first recommendation plan."
        action={<Link to="/tuning/new">Start tuning</Link>}
      />
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <div><h2 style={{ margin: 0, fontSize: '1.5rem', color: '#111827' }}>Tuning Sessions</h2>
          <span style={{ fontSize: '0.8rem', color: '#6b7280' }}>{history?.total ?? displayedCount} session{(history?.total ?? displayedCount) !== 1 ? 's' : ''}, including completed history</span>
        </div>
        <Link to="/tuning/new" style={{ padding: '9px 14px', background: '#2563eb', color: '#fff', borderRadius: '6px', textDecoration: 'none', fontWeight: 600 }}>Start tuning</Link>
      </div>
      {haltError && (
        <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#fef2f2', color: '#dc2626', borderRadius: '6px', fontSize: '0.85rem' }}>
          {haltError}
        </div>
      )}
      <form onSubmit={applyTextFilters} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(155px, 1fr))', gap: '9px', padding: '12px', marginBottom: '14px', background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: '8px' }}>
        <input aria-label="Search objective" placeholder="Search objective" value={draftObjective} onChange={(event) => setDraftObjective(event.target.value)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }} />
        <input aria-label="Filter database" placeholder="Database" value={draftDatabase} onChange={(event) => setDraftDatabase(event.target.value)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }} />
        <select aria-label="Filter host" value={filters.host_id ?? ''} onChange={(event) => setFilter('host_id', event.target.value || undefined)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }}>
          <option value="">All hosts</option>{(hosts ?? []).map((host) => <option key={host.id} value={host.id}>{host.hostname}</option>)}
        </select>
        <select aria-label="Filter status" value={filters.status?.[0] ?? ''} onChange={(event) => setFilter('status', event.target.value ? [event.target.value as RunStatus] : undefined)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }}>
          <option value="">All statuses</option>{['queued', 'running', 'waiting_approval', 'completed', 'failed', 'manually_halted', 'timed_out'].map((status) => <option key={status} value={status}>{status.replace(/_/g, ' ')}</option>)}
        </select>
        <select aria-label="Filter target" value={filters.tuning_target ?? ''} onChange={(event) => setFilter('tuning_target', (event.target.value || undefined) as TuningTarget | undefined)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }}>
          <option value="">All targets</option>{['system_wide_aqr', 'transactions_per_second', 'recommended_fingerprint', 'custom_fingerprint', 'composite'].map((target) => <option key={target} value={target}>{target.replace(/_/g, ' ')}</option>)}
        </select>
        <select aria-label="Filter mode" value={filters.tuning_mode ?? ''} onChange={(event) => setFilter('tuning_mode', (event.target.value || undefined) as TuningMode | undefined)} style={{ padding: '8px', border: '1px solid #d1d5db', borderRadius: '5px' }}>
          <option value="">All modes</option><option value="reload_only">Reload only</option><option value="restart_enabled">Restart enabled</option>
        </select>
        <label style={{ fontSize: '0.72rem', color: '#6b7280' }}>From<input type="date" value={filters.date_from?.slice(0, 10) ?? ''} onChange={(event) => setFilter('date_from', event.target.value ? `${event.target.value}T00:00:00Z` : undefined)} style={{ display: 'block', width: '100%', boxSizing: 'border-box', padding: '6px', border: '1px solid #d1d5db', borderRadius: '5px' }} /></label>
        <label style={{ fontSize: '0.72rem', color: '#6b7280' }}>To<input type="date" value={filters.date_to?.slice(0, 10) ?? ''} onChange={(event) => setFilter('date_to', event.target.value ? `${event.target.value}T23:59:59Z` : undefined)} style={{ display: 'block', width: '100%', boxSizing: 'border-box', padding: '6px', border: '1px solid #d1d5db', borderRadius: '5px' }} /></label>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'end' }}><button type="submit" style={{ padding: '8px 10px', background: '#111827', color: '#fff', border: 0, borderRadius: '5px' }}>Apply</button><button type="button" onClick={clearFilters} style={{ padding: '8px 10px', background: '#fff', border: '1px solid #d1d5db', borderRadius: '5px' }}>Clear</button></div>
      </form>
      <DataTable
        columns={columns}
        data={displayRuns ?? []}
        keyExtractor={(r) => r.id}
      />
      {displayRuns?.length === 0 && <div style={{ padding: '26px', textAlign: 'center', color: '#6b7280' }}>No sessions match these filters.</div>}
      {(history?.total_pages ?? 1) > 1 && <div style={{ display: 'flex', justifyContent: 'center', gap: '10px', alignItems: 'center', marginTop: '14px' }}>
        <button disabled={(history?.page ?? 1) <= 1} onClick={() => setFilters((current) => ({ ...current, page: (history?.page ?? 1) - 1 }))}>Previous</button>
        <span style={{ fontSize: '0.85rem' }}>Page {history?.page} of {history?.total_pages}</span>
        <button disabled={(history?.page ?? 1) >= (history?.total_pages ?? 1)} onClick={() => setFilters((current) => ({ ...current, page: (history?.page ?? 1) + 1 }))}>Next</button>
      </div>}
    </div>
  );
}
