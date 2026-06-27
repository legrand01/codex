import { useState } from 'react';
import { auditApi } from '../api/client';
import type { AuditEntry } from '../api/types';
import { useApi } from '../hooks/useApi';
import { DataTable, EmptyState, LoadingSpinner } from '../components';
import type { Column } from '../components';

function ResultBadge({ result }: { result: string }) {
  const colors: Record<string, { bg: string; text: string }> = {
    success: { bg: '#dcfce7', text: '#166534' },
    failure: { bg: '#fee2e2', text: '#991b1b' },
    blocked: { bg: '#fef9c3', text: '#854d0e' },
  };
  const c = colors[result] || colors.failure;
  return (
    <span style={{
      padding: '2px 6px',
      borderRadius: '4px',
      fontSize: '0.7rem',
      fontWeight: 500,
      backgroundColor: c.bg,
      color: c.text,
    }}>
      {result}
    </span>
  );
}

export function AuditLog() {
  const [runId, setRunId] = useState('');
  const [searchRunId, setSearchRunId] = useState('');

  const { data: entries, loading, error } = useApi<AuditEntry[]>(
    () => (searchRunId ? auditApi.getAuditLog(searchRunId) : Promise.resolve([])),
    [searchRunId],
  );

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setSearchRunId(runId.trim());
  };

  const columns: Column<AuditEntry>[] = [
    {
      key: 'timestamp',
      header: 'Timestamp',
      render: (e) => new Date(e.timestamp).toLocaleString(),
      width: '160px',
    },
    {
      key: 'actor',
      header: 'Actor',
      render: (e) => (
        <div>
          <span style={{ fontSize: '0.8rem', fontWeight: 500 }}>{e.actor_name}</span>
          <span style={{ fontSize: '0.7rem', color: '#6b7280', marginLeft: '4px' }}>
            ({e.actor_type})
          </span>
        </div>
      ),
    },
    {
      key: 'action_type',
      header: 'Action',
      render: (e) => (
        <span style={{ textTransform: 'capitalize' }}>
          {e.action_type.replace(/_/g, ' ')}
        </span>
      ),
    },
    {
      key: 'result',
      header: 'Result',
      render: (e) => <ResultBadge result={e.result} />,
    },
    {
      key: 'result_reason',
      header: 'Reason',
      render: (e) => (
        <span style={{ fontSize: '0.8rem', color: '#4b5563', maxWidth: '200px', display: 'inline-block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {e.result_reason || '-'}
        </span>
      ),
    },
  ];

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: '1.5rem', color: '#111827' }}>Audit Log</h2>

      <form onSubmit={handleSearch} style={{ marginBottom: '24px', display: 'flex', gap: '8px' }}>
        <input
          type="text"
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
          placeholder="Enter Run ID to view audit log..."
          style={{
            flex: 1,
            padding: '8px 12px',
            border: '1px solid #d1d5db',
            borderRadius: '6px',
            fontSize: '0.875rem',
          }}
        />
        <button
          type="submit"
          disabled={!runId.trim()}
          style={{
            padding: '8px 16px',
            backgroundColor: '#3b82f6',
            color: '#fff',
            border: 'none',
            borderRadius: '6px',
            cursor: 'pointer',
            fontSize: '0.875rem',
          }}
        >
          Load Audit Log
        </button>
      </form>

      {loading && <LoadingSpinner message="Loading audit entries..." />}
      {error && <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error}</div>}

      {!loading && !error && searchRunId && (!entries || entries.length === 0) && (
        <EmptyState
          title="No Audit Entries"
          description="No audit log entries found for the specified run."
        />
      )}

      {entries && entries.length > 0 && (
        <DataTable
          columns={columns}
          data={entries}
          keyExtractor={(e) => String(e.id)}
        />
      )}
    </div>
  );
}
