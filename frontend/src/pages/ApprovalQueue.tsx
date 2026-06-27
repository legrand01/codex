import { useState } from 'react';
import { plansApi } from '../api/client';
import type { PlanDetail, PaginatedResponse } from '../api/types';
import { useApi } from '../hooks/useApi';
import { StatusBadge, DataTable, EmptyState, LoadingSpinner, Pagination } from '../components';
import type { Column } from '../components';

export function ApprovalQueue() {
  const [page, setPage] = useState(1);
  const [selectedPlan, setSelectedPlan] = useState<PlanDetail | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [rejectError, setRejectError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const { data, loading, error, refetch } = useApi<PaginatedResponse<PlanDetail>>(
    () => plansApi.listPendingPlans(page, 50),
    [page],
  );

  const handleApprove = async (planId: string) => {
    setActionLoading(true);
    setActionMessage(null);
    try {
      const result = await plansApi.approvePlan(planId);
      setActionMessage(`Plan approved: ${result.status || 'forwarding to guardrails'}`);
      setSelectedPlan(null);
      refetch();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : 'Failed to approve plan');
    } finally {
      setActionLoading(false);
    }
  };

  const handleReject = async (planId: string) => {
    const trimmed = rejectReason.trim();
    if (trimmed.length < 10) {
      setRejectError('Rejection reason must be at least 10 characters.');
      return;
    }
    setRejectError(null);
    setActionLoading(true);
    setActionMessage(null);
    try {
      await plansApi.rejectPlan(planId, trimmed);
      setActionMessage('Plan rejected successfully.');
      setSelectedPlan(null);
      setRejectReason('');
      refetch();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : 'Failed to reject plan');
    } finally {
      setActionLoading(false);
    }
  };

  const columns: Column<PlanDetail>[] = [
    {
      key: 'id',
      header: 'Plan ID',
      render: (p) => <code style={{ fontSize: '0.75rem' }}>{p.id.slice(0, 8)}...</code>,
    },
    {
      key: 'status',
      header: 'Status',
      render: (p) => <StatusBadge type="plan" status={p.status} />,
    },
    {
      key: 'risk_score',
      header: 'Risk Score',
      render: (p) => (
        <span style={{ color: p.risk_score > 70 ? '#dc2626' : p.risk_score > 40 ? '#d97706' : '#16a34a', fontWeight: 600 }}>
          {p.risk_score}/100
        </span>
      ),
    },
    {
      key: 'changes',
      header: 'Changes',
      render: (p) => `${p.proposed_changes.length} change${p.proposed_changes.length !== 1 ? 's' : ''}`,
    },
    {
      key: 'submission_time',
      header: 'Submitted',
      render: (p) => new Date(p.submission_time).toLocaleString(),
    },
    {
      key: 'actions',
      header: '',
      render: (p) => (
        <button
          onClick={() => setSelectedPlan(p)}
          style={{
            padding: '4px 8px',
            fontSize: '0.75rem',
            backgroundColor: '#eff6ff',
            color: '#1d4ed8',
            border: '1px solid #bfdbfe',
            borderRadius: '4px',
            cursor: 'pointer',
          }}
        >
          Review
        </button>
      ),
    },
  ];

  if (loading) return <LoadingSpinner message="Loading plans..." />;
  if (error) return <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error} <button onClick={refetch}>Retry</button></div>;

  if (!data || data.items.length === 0) {
    return (
      <EmptyState
        title="No Plans Awaiting Approval"
        description="All plans have been reviewed. New plans will appear here when generated."
      />
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h2 style={{ margin: 0, fontSize: '1.5rem', color: '#111827' }}>Plan Approval Queue</h2>
        <span style={{ fontSize: '0.8rem', color: '#6b7280' }}>
          {data.total} plan{data.total !== 1 ? 's' : ''} pending
        </span>
      </div>

      {actionMessage && (
        <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#f0fdf4', color: '#166534', borderRadius: '6px', fontSize: '0.85rem', border: '1px solid #bbf7d0' }}>
          {actionMessage}
        </div>
      )}

      <DataTable
        columns={columns}
        data={data.items}
        keyExtractor={(p) => p.id}
      />

      <Pagination
        currentPage={page}
        totalPages={data.total_pages}
        onPageChange={setPage}
      />

      {/* Plan Detail Modal */}
      {selectedPlan && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: 'rgba(0,0,0,0.5)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 999,
        }}>
          <div style={{
            backgroundColor: '#fff',
            borderRadius: '12px',
            padding: '24px',
            width: '90%',
            maxWidth: '700px',
            maxHeight: '80vh',
            overflow: 'auto',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <h3 style={{ margin: 0 }}>Plan Details</h3>
              <button
                onClick={() => { setSelectedPlan(null); setRejectReason(''); setRejectError(null); }}
                style={{ background: 'none', border: 'none', fontSize: '1.5rem', cursor: 'pointer' }}
              >
                &times;
              </button>
            </div>

            <div style={{ display: 'grid', gap: '12px', marginBottom: '16px' }}>
              <div><strong>Plan ID:</strong> {selectedPlan.id}</div>
              <div><strong>Status:</strong> <StatusBadge type="plan" status={selectedPlan.status} /></div>
              <div><strong>Risk Score:</strong> <span style={{ color: selectedPlan.risk_score > 70 ? '#dc2626' : '#16a34a', fontWeight: 600 }}>{selectedPlan.risk_score}/100</span></div>
              <div><strong>Confidence:</strong> {(selectedPlan.confidence_score * 100).toFixed(0)}%</div>
              {selectedPlan.uncertainty_explanation && (
                <div><strong>Uncertainty:</strong> {selectedPlan.uncertainty_explanation}</div>
              )}
            </div>

            <div style={{ marginBottom: '16px' }}>
              <h4 style={{ margin: '0 0 8px' }}>Proposed Changes</h4>
              <pre style={{ backgroundColor: '#f3f4f6', padding: '12px', borderRadius: '6px', fontSize: '0.8rem', overflow: 'auto', maxHeight: '150px' }}>
                {JSON.stringify(selectedPlan.proposed_changes, null, 2)}
              </pre>
            </div>

            <div style={{ marginBottom: '16px' }}>
              <h4 style={{ margin: '0 0 8px' }}>Evidence References</h4>
              <div style={{ fontSize: '0.8rem', color: '#4b5563' }}>
                {selectedPlan.evidence_references.length > 0 ? (
                  selectedPlan.evidence_references.map((ref, i) => (
                    <a
                      key={i}
                      href={`#evidence-${(ref as { snapshot_id?: string }).snapshot_id || ''}`}
                      style={{ display: 'block', color: '#3b82f6', marginBottom: '4px' }}
                    >
                      Snapshot: {((ref as { snapshot_id?: string }).snapshot_id || 'unknown').toString().slice(0, 8)}...
                    </a>
                  ))
                ) : (
                  <span style={{ color: '#9ca3af' }}>No evidence references</span>
                )}
              </div>
            </div>

            <div style={{ marginBottom: '16px' }}>
              <h4 style={{ margin: '0 0 8px' }}>Rollback Instructions</h4>
              <pre style={{ backgroundColor: '#f3f4f6', padding: '12px', borderRadius: '6px', fontSize: '0.8rem', overflow: 'auto', maxHeight: '100px' }}>
                {JSON.stringify(selectedPlan.rollback_instructions, null, 2)}
              </pre>
            </div>

            {/* Approve / Reject Actions */}
            {selectedPlan.status === 'pending_approval' && (
              <div style={{ borderTop: '1px solid #e5e7eb', paddingTop: '16px' }}>
                <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
                  <button
                    onClick={() => handleApprove(selectedPlan.id)}
                    disabled={actionLoading}
                    style={{
                      padding: '8px 16px',
                      backgroundColor: '#16a34a',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '6px',
                      cursor: 'pointer',
                      fontWeight: 500,
                    }}
                  >
                    {actionLoading ? 'Processing...' : 'Approve (Dry-Run)'}
                  </button>
                </div>

                <div>
                  <label style={{ fontSize: '0.85rem', fontWeight: 500, display: 'block', marginBottom: '6px' }}>
                    Reject with reason (min 10 characters):
                  </label>
                  <textarea
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    placeholder="Enter rejection reason..."
                    rows={3}
                    style={{
                      width: '100%',
                      padding: '8px 12px',
                      border: `1px solid ${rejectError ? '#dc2626' : '#d1d5db'}`,
                      borderRadius: '6px',
                      fontSize: '0.85rem',
                      resize: 'vertical',
                    }}
                  />
                  {rejectError && (
                    <div style={{ color: '#dc2626', fontSize: '0.75rem', marginTop: '4px' }}>
                      {rejectError}
                    </div>
                  )}
                  <button
                    onClick={() => handleReject(selectedPlan.id)}
                    disabled={actionLoading}
                    style={{
                      marginTop: '8px',
                      padding: '8px 16px',
                      backgroundColor: '#dc2626',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '6px',
                      cursor: 'pointer',
                      fontWeight: 500,
                    }}
                  >
                    {actionLoading ? 'Processing...' : 'Reject'}
                  </button>
                </div>
              </div>
            )}

            {/* Forwarding states */}
            {selectedPlan.status === 'pending_forwarding' && (
              <div style={{ padding: '12px', backgroundColor: '#eff6ff', borderRadius: '6px', color: '#1e40af', fontSize: '0.85rem' }}>
                Plan is pending forwarding to the Guardrail Engine...
              </div>
            )}
            {selectedPlan.status === 'forwarding_failed' && (
              <div style={{ padding: '12px', backgroundColor: '#fef2f2', borderRadius: '6px', color: '#991b1b', fontSize: '0.85rem' }}>
                Forwarding failed. The Guardrail Engine could not be reached after retries.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
