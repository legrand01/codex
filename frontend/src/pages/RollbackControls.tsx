import { useState } from 'react';
import { rollbackApi } from '../api/client';
import type { RollbackStatusResponse, PlanStatus } from '../api/types';
import { StatusBadge, LoadingSpinner } from '../components';

const ELIGIBLE_STATUSES: PlanStatus[] = ['applied', 'rollback_failed'];

export function RollbackControls() {
  const [planId, setPlanId] = useState('');
  const [rollbackStatus, setRollbackStatus] = useState<RollbackStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const handleCheckStatus = async () => {
    if (!planId.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const status = await rollbackApi.getRollbackStatus(planId.trim());
      setRollbackStatus(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch rollback status');
      setRollbackStatus(null);
    } finally {
      setLoading(false);
    }
  };

  const handleInitiateRollback = async () => {
    if (!planId.trim()) return;
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await rollbackApi.initiateRollback(planId.trim());
      setMessage(`Rollback initiated: ${result.message || result.status}`);
      // Refresh status
      const status = await rollbackApi.getRollbackStatus(planId.trim());
      setRollbackStatus(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to initiate rollback');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: '1.5rem', color: '#111827' }}>Rollback Controls</h2>

      <div style={{ marginBottom: '24px', padding: '16px', border: '1px solid #e5e7eb', borderRadius: '8px', backgroundColor: '#f9fafb' }}>
        <p style={{ margin: '0 0 8px', fontSize: '0.85rem', color: '#4b5563' }}>
          Enter a Plan ID to check rollback eligibility and initiate rollback.
          Rollback is only available for plans with status <strong>&quot;applied&quot;</strong> or <strong>&quot;rollback failed&quot;</strong>.
        </p>
        <div style={{ display: 'flex', gap: '8px' }}>
          <input
            type="text"
            value={planId}
            onChange={(e) => setPlanId(e.target.value)}
            placeholder="Enter Plan ID..."
            style={{
              flex: 1,
              padding: '8px 12px',
              border: '1px solid #d1d5db',
              borderRadius: '6px',
              fontSize: '0.875rem',
            }}
          />
          <button
            onClick={handleCheckStatus}
            disabled={!planId.trim() || loading}
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
            Check Status
          </button>
        </div>
      </div>

      {loading && <LoadingSpinner message="Processing..." />}
      {error && <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#fef2f2', color: '#dc2626', borderRadius: '6px', fontSize: '0.85rem' }}>{error}</div>}
      {message && <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#f0fdf4', color: '#166534', borderRadius: '6px', fontSize: '0.85rem' }}>{message}</div>}

      {rollbackStatus && (
        <div style={{ padding: '16px', border: '1px solid #e5e7eb', borderRadius: '8px' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: '1rem' }}>Rollback Status</h3>
          <div style={{ display: 'grid', gap: '8px' }}>
            <div><strong>Plan ID:</strong> {rollbackStatus.plan_id}</div>
            <div>
              <strong>Status:</strong>{' '}
              {rollbackStatus.rollback_status === 'not_applicable' ? (
                <span style={{ color: '#6b7280', fontSize: '0.85rem' }}>not applicable</span>
              ) : (
                <StatusBadge type="rollback" status={rollbackStatus.rollback_status} />
              )}
            </div>
            <div>
              <strong>Plan Status:</strong>{' '}
              <StatusBadge type="plan" status={rollbackStatus.plan_status} />
            </div>
            {rollbackStatus.applied_at && (
              <div><strong>Applied:</strong> {new Date(rollbackStatus.applied_at).toLocaleString()}</div>
            )}
            {rollbackStatus.rolled_back_at && (
              <div><strong>Rolled Back:</strong> {new Date(rollbackStatus.rolled_back_at).toLocaleString()}</div>
            )}
          </div>

          {/* Show rollback button if eligible */}
          {(ELIGIBLE_STATUSES.includes(rollbackStatus.plan_status) ||
            rollbackStatus.rollback_status === 'failed') && (
            <button
              onClick={handleInitiateRollback}
              disabled={loading}
              style={{
                marginTop: '16px',
                padding: '8px 16px',
                backgroundColor: '#dc2626',
                color: '#fff',
                border: 'none',
                borderRadius: '6px',
                cursor: 'pointer',
                fontWeight: 500,
              }}
            >
              {loading ? 'Processing...' : 'Initiate Rollback'}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
