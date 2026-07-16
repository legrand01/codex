import { useState, useEffect } from 'react';
import { evidenceApi } from '../api/client';
import type { EvidenceListResponse, EvidenceSnapshotSummary } from '../api/types';
import { useApi } from '../hooks/useApi';
import { EmptyState, LoadingSpinner } from '../components';

const EVIDENCE_CATEGORIES: Record<string, string> = {
  pg_settings: 'Configuration',
  pg_stat_database: 'Performance',
  pg_stat_statements: 'Performance',
  locks: 'Locks',
  replication: 'Replication',
  wal_checkpoint: 'WAL/Checkpoint',
  os_metrics: 'OS Metrics',
};

function formatFreshness(collectedAt: string): string {
  const ageMs = Date.now() - new Date(collectedAt).getTime();
  const ageSec = Math.max(0, ageMs / 1000);
  if (ageSec < 60) return `${Math.floor(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  return `${Math.floor(ageSec / 3600)}h ago`;
}

interface CategoryGroup {
  category: string;
  label: string;
  count: number;
  snapshots: EvidenceSnapshotSummary[];
}

function groupByCategory(snapshots: EvidenceSnapshotSummary[]): CategoryGroup[] {
  const groups = new Map<string, EvidenceSnapshotSummary[]>();
  for (const s of snapshots) {
    const cat = EVIDENCE_CATEGORIES[s.evidence_type] || s.evidence_type;
    const existing = groups.get(cat) || [];
    existing.push(s);
    groups.set(cat, existing);
  }
  return Array.from(groups.entries()).map(([label, snaps]) => ({
    category: label,
    label,
    count: snaps.length,
    snapshots: snaps.sort(
      (a, b) => new Date(b.collected_at).getTime() - new Date(a.collected_at).getTime(),
    ),
  }));
}

export function EvidenceViewer() {
  const [runId, setRunId] = useState('');
  const [searchRunId, setSearchRunId] = useState('');
  const [freshness, setFreshness] = useState(0);

  const { data: evidencePage, loading, error } = useApi<EvidenceListResponse | null>(
    () => (searchRunId ? evidenceApi.listEvidencePage(searchRunId) : Promise.resolve(null)),
    [searchRunId],
  );

  // Update freshness every 30 seconds
  useEffect(() => {
    const timer = setInterval(() => {
      setFreshness((f) => f + 1);
    }, 30000);
    return () => clearInterval(timer);
  }, []);

  // Force re-render on freshness change (used in formatFreshness calls)
  void freshness;

  const evidence = evidencePage?.snapshots ?? [];
  const categories = groupByCategory(evidence);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setSearchRunId(runId.trim());
  };

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: '1.5rem', color: '#111827' }}>Evidence Viewer</h2>

      <form onSubmit={handleSearch} style={{ marginBottom: '24px', display: 'flex', gap: '8px' }}>
        <input
          type="text"
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
          placeholder="Enter Run ID to view evidence..."
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
          Load Evidence
        </button>
      </form>

      {loading && <LoadingSpinner message="Loading evidence..." />}
      {error && <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error}</div>}

      {!loading && !error && searchRunId && evidence.length === 0 && (
        <EmptyState
          title="No Evidence Collected"
          description="No evidence has been collected yet for the selected run."
        />
      )}

      {categories.length > 0 && (
        <div>
          {/* Category summary */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', marginBottom: '24px' }}>
            {(evidencePage?.categories ?? []).map((cat) => (
              <div
                key={cat.category}
                style={{
                  padding: '12px 16px',
                  border: '1px solid #e5e7eb',
                  borderRadius: '8px',
                  backgroundColor: '#f9fafb',
                  minWidth: '140px',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: '0.875rem', color: '#374151' }}>
                  {cat.category}
                </div>
                <div style={{ fontSize: '1.25rem', fontWeight: 700, color: '#3b82f6' }}>
                  {cat.count.toLocaleString()}
                </div>
                <div style={{ fontSize: '0.7rem', color: '#6b7280' }}>snapshots</div>
              </div>
            ))}
          </div>

          {evidencePage && evidencePage.total > evidence.length && <div style={{ color: '#6b7280', fontSize: '0.8rem', marginBottom: '16px' }}>Showing the newest {evidence.length} of {evidencePage.total.toLocaleString()} snapshots.</div>}

          {/* Evidence details by category */}
          {categories.map((cat) => (
            <div key={cat.category} style={{ marginBottom: '24px' }}>
              <h3 style={{ margin: '0 0 12px', fontSize: '1rem', color: '#374151' }}>
                {cat.label} ({cat.count})
              </h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {cat.snapshots.map((snap) => (
                  <div
                    key={snap.id}
                    id={`evidence-${snap.id}`}
                    style={{
                      padding: '12px 16px',
                      border: '1px solid #e5e7eb',
                      borderRadius: '6px',
                      backgroundColor: '#ffffff',
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                    }}
                  >
                    <div>
                      <div style={{ fontWeight: 500, fontSize: '0.875rem' }}>
                        {snap.evidence_type}
                      </div>
                      <div style={{ fontSize: '0.75rem', color: '#6b7280' }}>
                        ID: {snap.id.slice(0, 8)}...
                      </div>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <div style={{ fontSize: '0.8rem', color: '#6b7280' }}>
                        {formatFreshness(snap.collected_at)}
                      </div>
                      {snap.quality_score !== null && (
                        <div style={{ fontSize: '0.7rem', color: snap.quality_score >= 0.7 ? '#16a34a' : '#d97706' }}>
                          Quality: {(snap.quality_score * 100).toFixed(0)}%
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
