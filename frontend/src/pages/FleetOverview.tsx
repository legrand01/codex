import { useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { fleetApi } from '../api/client';
import type { HostSummary, WSFleetUpdate } from '../api/types';
import { useApi } from '../hooks/useApi';
import { useWebSocket } from '../hooks/useWebSocket';
import { StatusBadge, DataTable, EmptyState, LoadingSpinner } from '../components';
import type { Column } from '../components';

export function FleetOverview() {
  const { data: hosts, loading, error, refetch } = useApi<HostSummary[]>(
    () => fleetApi.listHosts(),
    [],
  );
  const [hostList, setHostList] = useState<HostSummary[] | null>(null);

  const handleWSMessage = useCallback((msg: unknown) => {
    const update = msg as WSFleetUpdate;
    if (update.type === 'host_update' && update.host) {
      setHostList((prev) => {
        const current = prev || hosts || [];
        const idx = current.findIndex((h) => h.id === update.host.id);
        if (idx >= 0) {
          const updated = [...current];
          updated[idx] = update.host;
          return updated;
        }
        return [...current, update.host];
      });
    }
  }, [hosts]);

  useWebSocket({
    url: '/ws/fleet',
    onMessage: handleWSMessage,
  });

  const displayHosts = hostList || hosts;

  const columns: Column<HostSummary>[] = [
    {
      key: 'hostname',
      header: 'Hostname',
      render: (h) => <strong>{h.hostname}</strong>,
    },
    {
      key: 'health_status',
      header: 'Health',
      render: (h) => <StatusBadge type="health" status={h.health_status} />,
    },
    {
      key: 'connection_status',
      header: 'Connection',
      render: (h) => <StatusBadge type="connection" status={h.connection_status} />,
    },
    {
      key: 'pg_version',
      header: 'PG Version',
      render: (h) => h.pg_version || 'N/A',
    },
    {
      key: 'server_role',
      header: 'Role',
      render: (h) => (
        <span style={{ textTransform: 'capitalize' }}>
          {h.server_role || 'Unknown'}
        </span>
      ),
    },
    {
      key: 'last_heartbeat',
      header: 'Last Heartbeat',
      render: (h) => h.last_heartbeat
        ? new Date(h.last_heartbeat).toLocaleTimeString()
        : 'Never',
    },
  ];

  if (loading) return <LoadingSpinner message="Loading fleet..." />;
  if (error) return <div style={{ color: '#dc2626', padding: '16px' }}>Error: {error} <button onClick={refetch}>Retry</button></div>;
  if (!displayHosts || displayHosts.length === 0) {
    return (
      <EmptyState
        title="No PostgreSQL Hosts Registered"
        description="Register hosts to start monitoring your PostgreSQL fleet."
      />
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h2 style={{ margin: 0, fontSize: '1.5rem', color: '#111827' }}>Fleet Overview</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <span style={{ fontSize: '0.8rem', color: '#6b7280' }}>{displayHosts.length} host{displayHosts.length !== 1 ? 's' : ''} registered</span>
          <Link to="/tuning/new" style={{ padding: '9px 14px', background: '#2563eb', color: '#fff', borderRadius: '6px', textDecoration: 'none', fontWeight: 600 }}>Start tuning</Link>
        </div>
      </div>
      <DataTable
        columns={columns}
        data={displayHosts}
        keyExtractor={(h) => h.id}
      />
    </div>
  );
}
