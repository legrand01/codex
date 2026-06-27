import { useState } from 'react';
import { reportsApi } from '../api/client';
import type { DBAReport, ReportSearchQuery } from '../api/types';
import { useApi } from '../hooks/useApi';
import { EmptyState, LoadingSpinner } from '../components';

function OutcomeStatusBadge({ status }: { status: string }) {
  const colors: Record<string, { bg: string; text: string }> = {
    success: { bg: '#dcfce7', text: '#166534' },
    partial_success: { bg: '#fef9c3', text: '#854d0e' },
    failure: { bg: '#fee2e2', text: '#991b1b' },
  };
  const c = colors[status] || colors.failure;
  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: '9999px',
      fontSize: '0.75rem',
      fontWeight: 500,
      backgroundColor: c.bg,
      color: c.text,
    }}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}

export function ReportsViewer() {
  const [searchQuery, setSearchQuery] = useState<ReportSearchQuery>({});
  const [keywords, setKeywords] = useState('');
  const [hostId, setHostId] = useState('');
  const [runIdDirect, setRunIdDirect] = useState('');
  const [triggerSearch, setTriggerSearch] = useState(false);
  const [directReport, setDirectReport] = useState<DBAReport | null>(null);
  const [directLoading, setDirectLoading] = useState(false);
  const [directError, setDirectError] = useState<string | null>(null);

  const { data: reports, loading, error } = useApi<DBAReport[]>(
    () => (triggerSearch ? reportsApi.searchReports(searchQuery) : Promise.resolve([])),
    [triggerSearch, searchQuery],
  );

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const query: ReportSearchQuery = {};
    if (keywords.trim()) query.keywords = keywords.trim();
    if (hostId.trim()) query.host_id = hostId.trim();
    setSearchQuery(query);
    setTriggerSearch(true);
  };

  const handleDirectLoad = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!runIdDirect.trim()) return;
    setDirectLoading(true);
    setDirectError(null);
    try {
      const report = await reportsApi.getReport(runIdDirect.trim());
      setDirectReport(report);
    } catch (err) {
      setDirectError(err instanceof Error ? err.message : 'Failed to load report');
      setDirectReport(null);
    } finally {
      setDirectLoading(false);
    }
  };

  const renderReport = (report: DBAReport) => (
    <div
      key={report.id}
      style={{
        border: '1px solid #e5e7eb',
        borderRadius: '8px',
        padding: '16px',
        marginBottom: '16px',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
        <div>
          <h3 style={{ margin: 0, fontSize: '1rem' }}>{report.goal}</h3>
          <div style={{ fontSize: '0.75rem', color: '#6b7280', marginTop: '4px' }}>
            Run: {report.run_id.slice(0, 8)}... | Generated: {new Date(report.generated_at).toLocaleString()}
          </div>
        </div>
        <OutcomeStatusBadge status={report.outcome_status} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        <div style={{ padding: '8px', backgroundColor: '#f9fafb', borderRadius: '6px' }}>
          <div style={{ fontSize: '0.7rem', color: '#6b7280', textTransform: 'uppercase' }}>Evidence</div>
          <div style={{ fontWeight: 600 }}>{report.evidence_summaries.length} items</div>
        </div>
        <div style={{ padding: '8px', backgroundColor: '#f9fafb', borderRadius: '6px' }}>
          <div style={{ fontSize: '0.7rem', color: '#6b7280', textTransform: 'uppercase' }}>Plans</div>
          <div style={{ fontWeight: 600 }}>{report.plans_proposed.length} proposed</div>
        </div>
        <div style={{ padding: '8px', backgroundColor: '#f9fafb', borderRadius: '6px' }}>
          <div style={{ fontSize: '0.7rem', color: '#6b7280', textTransform: 'uppercase' }}>Changes</div>
          <div style={{ fontWeight: 600 }}>{report.applied_changes.length} applied</div>
        </div>
        <div style={{ padding: '8px', backgroundColor: '#f9fafb', borderRadius: '6px' }}>
          <div style={{ fontSize: '0.7rem', color: '#6b7280', textTransform: 'uppercase' }}>Verifications</div>
          <div style={{ fontWeight: 600 }}>{report.verification_results.length} results</div>
        </div>
      </div>

      {report.approval_decisions.length > 0 && (
        <div>
          <h4 style={{ margin: '0 0 8px', fontSize: '0.85rem' }}>Approval Decisions</h4>
          <pre style={{ backgroundColor: '#f3f4f6', padding: '8px', borderRadius: '4px', fontSize: '0.75rem', overflow: 'auto', maxHeight: '100px' }}>
            {JSON.stringify(report.approval_decisions, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: '1.5rem', color: '#111827' }}>DBA Reports</h2>

      {/* Direct report load */}
      <div style={{ marginBottom: '24px', padding: '16px', border: '1px solid #e5e7eb', borderRadius: '8px', backgroundColor: '#f9fafb' }}>
        <h3 style={{ margin: '0 0 8px', fontSize: '0.9rem' }}>Load Report by Run ID</h3>
        <form onSubmit={handleDirectLoad} style={{ display: 'flex', gap: '8px' }}>
          <input
            type="text"
            value={runIdDirect}
            onChange={(e) => setRunIdDirect(e.target.value)}
            placeholder="Enter Run ID..."
            style={{ flex: 1, padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: '6px', fontSize: '0.875rem' }}
          />
          <button
            type="submit"
            disabled={!runIdDirect.trim() || directLoading}
            style={{ padding: '8px 16px', backgroundColor: '#3b82f6', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '0.875rem' }}
          >
            Load
          </button>
        </form>
      </div>

      {directLoading && <LoadingSpinner message="Loading report..." />}
      {directError && <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#fef2f2', color: '#dc2626', borderRadius: '6px', fontSize: '0.85rem' }}>{directError}</div>}
      {directReport && renderReport(directReport)}

      {/* Search reports */}
      <div style={{ marginBottom: '24px', padding: '16px', border: '1px solid #e5e7eb', borderRadius: '8px', backgroundColor: '#f9fafb' }}>
        <h3 style={{ margin: '0 0 8px', fontSize: '0.9rem' }}>Search Reports</h3>
        <form onSubmit={handleSearch} style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
          <input
            type="text"
            value={keywords}
            onChange={(e) => setKeywords(e.target.value)}
            placeholder="Keywords..."
            style={{ flex: 1, minWidth: '150px', padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: '6px', fontSize: '0.875rem' }}
          />
          <input
            type="text"
            value={hostId}
            onChange={(e) => setHostId(e.target.value)}
            placeholder="Host ID..."
            style={{ flex: 1, minWidth: '150px', padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: '6px', fontSize: '0.875rem' }}
          />
          <button
            type="submit"
            style={{ padding: '8px 16px', backgroundColor: '#3b82f6', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '0.875rem' }}
          >
            Search
          </button>
        </form>
      </div>

      {loading && <LoadingSpinner message="Searching reports..." />}
      {error && <div style={{ marginBottom: '12px', color: '#dc2626' }}>Error: {error}</div>}

      {!loading && !error && triggerSearch && (!reports || reports.length === 0) && (
        <EmptyState
          title="No Reports Found"
          description="No reports match your search criteria."
        />
      )}

      {reports && reports.length > 0 && (
        <div>
          <div style={{ fontSize: '0.8rem', color: '#6b7280', marginBottom: '12px' }}>
            Found {reports.length} report{reports.length !== 1 ? 's' : ''}
          </div>
          {reports.map(renderReport)}
        </div>
      )}
    </div>
  );
}
