import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { fleetApi } from '../api/client';
import type { CapabilityDiagnostic, SetupGuide } from '../api/types';
import { LoadingSpinner } from '../components';
import { useApi } from '../hooks/useApi';

export function HostDiagnostics() {
  const { hostId = '' } = useParams();
  const [mode, setMode] = useState('reload_only');
  const diagnostics = useApi<CapabilityDiagnostic>(() => fleetApi.getDiagnostics(hostId), [hostId]);
  const setup = useApi<SetupGuide>(() => fleetApi.getSetup(hostId, mode), [hostId, mode]);
  if (diagnostics.loading) return <LoadingSpinner message="Loading Host Agent diagnostics..." />;
  if (!diagnostics.data) return <div style={{ color: '#b91c1c' }}>{diagnostics.error}</div>;
  const data = diagnostics.data;
  return <div>
    <Link to="/">← Fleet</Link><h2>{data.hostname} setup and capabilities</h2>
    {data.agent_write_ambiguous && <div style={{ padding: '12px', background: '#fee2e2', color: '#991b1b', marginBottom: '12px' }}><strong>Writes blocked:</strong> multiple active agents are using this host identity.</div>}
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: '8px' }}>{Object.entries(data.capabilities).map(([name, available]) => <div key={name} style={{ border: '1px solid #e5e7eb', padding: '10px', borderRadius: '6px' }}><strong>{name.replace(/_/g, ' ')}</strong><div style={{ color: available ? '#15803d' : '#b91c1c' }}>{available ? 'Available' : 'Unavailable'}</div></div>)}</div>
    <h3>Least-privilege setup</h3>
    <select value={mode} onChange={(event) => setMode(event.target.value)}><option value="reload_only">Reload only</option><option value="restart_enabled">Restart enabled</option></select>
    {setup.data && <div>
      <h4>SQL</h4><pre style={{ whiteSpace: 'pre-wrap', background: '#f8fafc', padding: '12px' }}>{setup.data.sql.join('\n')}</pre>
      {[...setup.data.file_instructions, ...setup.data.provider_instructions, ...setup.data.cautions].map((item) => <p key={item}>• {item}</p>)}
    </div>}
  </div>;
}
