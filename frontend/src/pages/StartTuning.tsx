import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { fleetApi, runsApi } from '../api/client';
import type {
  HostSummary,
  TuningMode,
  TuningPreflight,
  TuningTarget,
} from '../api/types';
import { useApi } from '../hooks/useApi';
import { LoadingSpinner } from '../components';

const targetOptions: Array<{ value: TuningTarget; label: string; detail: string }> = [
  { value: 'system_wide_aqr', label: 'System-wide query latency', detail: 'Improve average query runtime across the measured workload.' },
  { value: 'transactions_per_second', label: 'Transactions per second', detail: 'Increase throughput while protecting latency and host health.' },
  { value: 'recommended_fingerprint', label: 'Recommended workload fingerprint', detail: 'Let DBTune select the highest-impact stable query family.' },
  { value: 'custom_fingerprint', label: 'Specific workload fingerprint', detail: 'Tune one fingerprint selected by its identifier.' },
  { value: 'composite', label: 'Balanced composite', detail: 'Balance query latency, throughput, and system guardrails.' },
];

const guardrailFields = [
  ['average_query_runtime_degradation_pct', 'Average query runtime regression', '%', 10],
  ['transactions_per_second_degradation_pct', 'Transactions/sec regression', '%', 10],
  ['fingerprint_runtime_degradation_pct', 'Fingerprint runtime regression', '%', 10],
  ['locks_increase_pct', 'Lock wait increase', '%', 20],
  ['replication_lag_seconds', 'Maximum replication lag', 'seconds', 30],
  ['wal_checkpoint_increase_pct', 'WAL/checkpoint increase', '%', 25],
  ['cpu_utilization_pct', 'Maximum CPU utilization', '%', 90],
  ['memory_utilization_pct', 'Maximum memory utilization', '%', 90],
  ['io_utilization_pct', 'Maximum I/O utilization', '%', 90],
] as const;

const sectionStyle: React.CSSProperties = {
  border: '1px solid #e5e7eb', borderRadius: '10px', padding: '18px', background: '#fff',
};
const inputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', padding: '10px', border: '1px solid #d1d5db', borderRadius: '6px', background: '#fff',
};

export function StartTuning() {
  const navigate = useNavigate();
  const { data: hosts, loading, error } = useApi<HostSummary[]>(() => fleetApi.listHosts(), []);
  const [hostId, setHostId] = useState('');
  const [mode, setMode] = useState<TuningMode>('reload_only');
  const [target, setTarget] = useState<TuningTarget>('system_wide_aqr');
  const [databaseName, setDatabaseName] = useState('');
  const [fingerprintId, setFingerprintId] = useState('');
  const [goal, setGoal] = useState('Improve PostgreSQL performance safely and report measurable results');
  const [selectedParameters, setSelectedParameters] = useState<string[]>([]);
  const [approvalPolicy, setApprovalPolicy] = useState<'per_candidate' | 'final_only'>('per_candidate');
  const [warmupSeconds, setWarmupSeconds] = useState(60);
  const [measurementSeconds, setMeasurementSeconds] = useState(300);
  const [guardrails, setGuardrails] = useState<Record<string, number>>(
    Object.fromEntries(guardrailFields.map(([key, , , value]) => [key, value])),
  );
  const [preflight, setPreflight] = useState<TuningPreflight | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [refreshSequence, setRefreshSequence] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!hostId && hosts?.length) setHostId(hosts[0].id);
  }, [hostId, hosts]);

  useEffect(() => {
    if (!hostId) {
      setPreflight(null);
      return;
    }
    let cancelled = false;
    setPreflightLoading(true);
    setPreflightError(null);
    runsApi.getPreflight(hostId, mode)
      .then((result) => {
        if (cancelled) return;
        setPreflight(result);
        setDatabaseName((current) => current || result.database_name || '');
        const available = new Set(result.parameters.filter((item) => item.available).map((item) => item.name));
        setSelectedParameters((current) => {
          const retained = current.filter((name) => available.has(name));
          return retained.length ? retained : Array.from(available);
        });
      })
      .catch((err) => {
        if (!cancelled) {
          setPreflight(null);
          setPreflightError(err instanceof Error ? err.message : 'Capability preflight failed');
        }
      })
      .finally(() => { if (!cancelled) setPreflightLoading(false); });
    return () => { cancelled = true; };
  }, [hostId, mode, refreshSequence]);

  const availableParameters = useMemo(
    () => preflight?.parameters.filter((parameter) => parameter.available) ?? [],
    [preflight],
  );
  const requiresFingerprint = target === 'custom_fingerprint';
  const canSubmit = Boolean(
    preflight?.ready
    && goal.trim()
    && hostId
    && selectedParameters.length
    && (!requiresFingerprint || fingerprintId.trim()),
  );

  const toggleParameter = (name: string) => {
    setSelectedParameters((current) => current.includes(name)
      ? current.filter((parameter) => parameter !== name)
      : [...current, name]);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await runsApi.startRun({
        goal: goal.trim(), host_id: hostId, database_name: databaseName.trim() || undefined,
        tuning_target: target, tuning_mode: mode,
        workload_fingerprint_id: requiresFingerprint ? fingerprintId.trim() : undefined,
        selected_parameters: selectedParameters, approval_policy: approvalPolicy,
        warmup_window_seconds: warmupSeconds, measurement_window_seconds: measurementSeconds,
        objective_guardrails: guardrails,
      });
      navigate(`/tuning/${result.run_id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Failed to start tuning');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <LoadingSpinner message="Loading database targets..." />;

  return <div style={{ maxWidth: '1040px', margin: '0 auto' }}>
    <Link to="/runs" style={{ color: '#2563eb', fontSize: '0.85rem' }}>← Tuning sessions</Link>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'start' }}>
      <div><h2 style={{ marginBottom: '4px' }}>Start tuning</h2>
        <p style={{ marginTop: 0, color: '#6b7280' }}>Configure one measured session. Start remains locked until the target agent proves every safety prerequisite.</p>
      </div>
      <button type="button" onClick={() => setRefreshSequence((value) => value + 1)} disabled={!hostId || preflightLoading}
        style={{ marginTop: '20px', padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: '6px', background: '#fff' }}>
        {preflightLoading ? 'Checking…' : 'Refresh checks'}
      </button>
    </div>

    <form onSubmit={submit} style={{ display: 'grid', gap: '16px' }}>
      <section style={sectionStyle}>
        <h3 style={{ marginTop: 0 }}>1. Target and objective</h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '14px' }}>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Database host</span>
            <select value={hostId} onChange={(event) => { setHostId(event.target.value); setDatabaseName(''); }} style={inputStyle}>
              <option value="" disabled>Select a registered target</option>
              {(hosts ?? []).map((host) => <option key={host.id} value={host.id}>{host.hostname} · {host.connection_status}</option>)}
            </select>
          </label>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Database</span>
            <input value={databaseName} onChange={(event) => setDatabaseName(event.target.value)} placeholder="Database reported by the agent" style={inputStyle} />
          </label>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Apply mode</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as TuningMode)} style={inputStyle}>
              <option value="reload_only">Reload only — no restart</option>
              <option value="restart_enabled">Restart enabled — explicitly enrolled</option>
            </select>
          </label>
        </div>
        <div style={{ marginTop: '16px' }}><span style={{ display: 'block', fontWeight: 600, marginBottom: '8px' }}>Optimization target</span>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '8px' }}>
            {targetOptions.map((option) => <label key={option.value} style={{ display: 'flex', gap: '9px', padding: '10px', border: `1px solid ${target === option.value ? '#2563eb' : '#e5e7eb'}`, borderRadius: '7px', cursor: 'pointer' }}>
              <input type="radio" checked={target === option.value} onChange={() => setTarget(option.value)} />
              <span><strong style={{ display: 'block' }}>{option.label}</strong><small style={{ color: '#6b7280' }}>{option.detail}</small></span>
            </label>)}
          </div>
        </div>
        {requiresFingerprint && <label style={{ display: 'block', marginTop: '12px' }}><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Workload fingerprint ID</span>
          <input value={fingerprintId} onChange={(event) => setFingerprintId(event.target.value)} placeholder="UUID from workload fingerprints" style={inputStyle} />
        </label>}
        <label style={{ display: 'block', marginTop: '14px' }}><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Session goal</span>
          <textarea value={goal} onChange={(event) => setGoal(event.target.value)} rows={3} style={{ ...inputStyle, resize: 'vertical' }} />
        </label>
      </section>

      <section style={sectionStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}><h3 style={{ margin: 0 }}>2. Capability preflight</h3>
          {preflight && <strong style={{ color: preflight.ready ? '#15803d' : '#b91c1c' }}>{preflight.ready ? 'READY' : 'BLOCKED'}</strong>}
        </div>
        {(error || preflightError) && <div style={{ color: '#b91c1c', marginTop: '12px' }}>{preflightError || error}</div>}
        {preflightLoading && <p style={{ color: '#6b7280' }}>Checking agent, PostgreSQL, telemetry, privileges, and configuration backend…</p>}
        {preflight && <>
          <p style={{ color: '#6b7280', fontSize: '0.85rem' }}>{preflight.hostname} · {preflight.pg_version || 'version unknown'} · {preflight.configuration_backend} · capabilities observed {preflight.capability_observed_at ? new Date(preflight.capability_observed_at).toLocaleString() : 'never'}</p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: '8px' }}>
            {preflight.checks.map((check) => <div key={check.key} style={{ padding: '9px', borderRadius: '6px', background: check.status === 'passed' ? '#f0fdf4' : check.status === 'warning' ? '#fffbeb' : '#fef2f2' }}>
              <strong style={{ color: check.status === 'passed' ? '#166534' : check.status === 'warning' ? '#92400e' : '#991b1b' }}>{check.status === 'passed' ? '✓' : check.status === 'warning' ? '!' : '×'} {check.label}</strong>
              <small style={{ display: 'block', marginTop: '3px', color: '#4b5563' }}>{check.message}</small>
            </div>)}
          </div>
        </>}
      </section>

      <section style={sectionStyle}>
        <h3 style={{ marginTop: 0 }}>3. Parameters</h3>
        <p style={{ color: '#6b7280', fontSize: '0.85rem' }}>Only parameters independently allowlisted for this host and supported by the selected mode can be included.</p>
        {preflight && <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '7px' }}>
          {preflight.parameters.map((parameter) => <label key={parameter.name} title={parameter.reason} style={{ display: 'flex', alignItems: 'center', gap: '8px', opacity: parameter.available ? 1 : 0.55, padding: '7px', border: '1px solid #e5e7eb', borderRadius: '6px' }}>
            <input type="checkbox" disabled={!parameter.available} checked={selectedParameters.includes(parameter.name)} onChange={() => toggleParameter(parameter.name)} />
            <code>{parameter.name}</code><small style={{ marginLeft: 'auto', color: '#6b7280' }}>{parameter.context}</small>
          </label>)}
        </div>}
        {preflight && availableParameters.length === 0 && <p style={{ color: '#b91c1c' }}>No parameter is currently available. Add a host allowlist before starting.</p>}
      </section>

      <section style={sectionStyle}>
        <h3 style={{ marginTop: 0 }}>4. Measurement and approvals</h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '14px' }}>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Approval policy</span>
            <select value={approvalPolicy} onChange={(event) => setApprovalPolicy(event.target.value as 'per_candidate' | 'final_only')} style={inputStyle}>
              <option value="per_candidate">Approve every candidate</option><option value="final_only">Final approval only</option>
            </select>
          </label>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Warm-up window (seconds)</span>
            <input type="number" min={0} max={3600} value={warmupSeconds} onChange={(event) => setWarmupSeconds(Number(event.target.value))} style={inputStyle} />
          </label>
          <label><span style={{ display: 'block', fontWeight: 600, marginBottom: '6px' }}>Measurement window (seconds)</span>
            <input type="number" min={30} max={86400} value={measurementSeconds} onChange={(event) => setMeasurementSeconds(Number(event.target.value))} style={inputStyle} />
          </label>
        </div>
        <h4>Regression guardrails</h4>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: '10px' }}>
          {guardrailFields.map(([key, label, unit]) => <label key={key}><span style={{ display: 'block', fontSize: '0.8rem', marginBottom: '4px' }}>{label} ({unit})</span>
            <input type="number" min={0} value={guardrails[key]} onChange={(event) => setGuardrails((current) => ({ ...current, [key]: Number(event.target.value) }))} style={inputStyle} />
          </label>)}
        </div>
      </section>

      {submitError && <div style={{ padding: '10px', color: '#b91c1c', background: '#fef2f2', borderRadius: '6px' }}>{submitError}</div>}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingBottom: '28px' }}>
        <span style={{ color: '#6b7280', fontSize: '0.85rem' }}>{selectedParameters.length} parameter{selectedParameters.length === 1 ? '' : 's'} selected</span>
        <button type="submit" disabled={submitting || !canSubmit} style={{ padding: '11px 18px', background: canSubmit ? '#2563eb' : '#9ca3af', color: '#fff', border: 0, borderRadius: '6px', fontWeight: 600, cursor: canSubmit ? 'pointer' : 'not-allowed' }}>
          {submitting ? 'Starting…' : preflight?.ready ? 'Start measured tuning' : 'Resolve blockers to start'}
        </button>
      </div>
    </form>
  </div>;
}
