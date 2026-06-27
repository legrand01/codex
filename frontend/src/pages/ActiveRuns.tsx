import { useState, useCallback } from 'react';
import { runsApi } from '../api/client';
import type { RunSummary, WSRunUpdate } from '../api/types';
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
  const { data: runs, loading, error, refetch } = useApi<RunSummary[]>(
    () => runsApi.listActiveRuns(),
    [],
  );
  const [runList, setRunList] = useState<RunSummary[] | null>(null);
  const [haltingId, setHaltingId] = useState<string | null>(null);
  const [haltConfirmId, setHaltConfirmId] = useState<string | null>(null);
  const [haltError, setHaltError] = useState<string | null>(null);

  const handleWSMessage = useCallback((msg: unknown) => {
    const update = msg as WSRunUpdate;
    if (update.run_id) {
      setRunList((prev) => {
        const current = prev || runs || [];
        return current.map((r) =>
          r.id === update.run_id
            ? { ...r, ...(update.data as Partial<RunSummary>) }
            : r,
        );
      });
    }
  }, [runs]);

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
        const current = prev || runs || [];
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

  const displayRuns = runList || runs;

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
        if (r.status !== 'running') {
          return <span style={{ color: '#9ca3af', fontSize: '0.8rem' }}>N/A</span>;
        }
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
        return (
          <button
            onClick={() => setHaltConfirmId(r.id)}
            style={{
              padding: '4px 8px',
              fontSize: '0.75rem',
              backgroundColor: '#fef2f2',
              color: '#dc2626',
              border: '1px solid #fecaca',
              borderRadius: '4px',
              cursor: 'pointer',
            }}
          >
            Halt
          </button>
        );
      },
    },
  ];

  if (loading) return <LoadingSpinner message="Loading active runs..." />;
  if (error) return <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error} <button onClick={refetch}>Retry</button></div>;
  if (!displayRuns || displayRuns.length === 0) {
    return (
      <EmptyState
        title="No Active Runs"
        description="Start a new DBA loop run to begin autonomous PostgreSQL investigation."
      />
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h2 style={{ margin: 0, fontSize: '1.5rem', color: '#111827' }}>Active Runs</h2>
        <span style={{ fontSize: '0.8rem', color: '#6b7280' }}>
          {displayRuns.length} active run{displayRuns.length !== 1 ? 's' : ''}
        </span>
      </div>
      {haltError && (
        <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#fef2f2', color: '#dc2626', borderRadius: '6px', fontSize: '0.85rem' }}>
          {haltError}
        </div>
      )}
      <DataTable
        columns={columns}
        data={displayRuns}
        keyExtractor={(r) => r.id}
      />
    </div>
  );
}
