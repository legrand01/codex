import { useState } from 'react';
import { Link } from 'react-router-dom';
import { eventsApi } from '../api/client';
import type { OperationalEvent } from '../api/types';
import { EmptyState, LoadingSpinner } from '../components';
import { useApi } from '../hooks/useApi';

export function Events() {
  const [query, setQuery] = useState('');
  const [severity, setSeverity] = useState('');
  const [component, setComponent] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const request = useApi<OperationalEvent[]>(() => eventsApi.list(filters), [filters]);
  const apply = () => setFilters({
    ...(query.trim() ? { q: query.trim() } : {}),
    ...(severity ? { severity } : {}),
    ...(component ? { component } : {}),
  });
  return <div>
    <h2>Operational events</h2>
    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '14px' }}>
      <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search code, message, or details" />
      <select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="">All severities</option>{['info', 'warning', 'error', 'critical'].map((value) => <option key={value}>{value}</option>)}</select>
      <input value={component} onChange={(event) => setComponent(event.target.value)} placeholder="Component" />
      <button onClick={apply}>Filter</button>
    </div>
    {request.loading && <LoadingSpinner message="Loading operational events..." />}
    {!request.loading && !request.data?.length && <EmptyState title="No matching operational events" />}
    <div style={{ display: 'grid', gap: '8px' }}>{request.data?.map((event) => <div key={event.id} style={{ border: '1px solid #e5e7eb', borderLeft: `4px solid ${event.severity === 'critical' || event.severity === 'error' ? '#dc2626' : event.severity === 'warning' ? '#d97706' : '#2563eb'}`, borderRadius: '7px', padding: '12px', background: '#fff' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}><strong>{event.event_code}</strong><span>{new Date(event.occurred_at).toLocaleString()}</span></div>
      <div>{event.message}</div><small>{event.component} · {event.severity} · {event.host_name ?? 'no host'}</small>
      {event.run_id && <div><Link to={`/tuning/${event.run_id}?tab=activity`}>Open tuning session</Link></div>}
    </div>)}</div>
  </div>;
}
