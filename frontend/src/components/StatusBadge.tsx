import type { HealthStatus, ConnectionStatus, PlanStatus, RunStatus, RollbackStatus } from '../api/types';

type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

const VARIANT_STYLES: Record<BadgeVariant, React.CSSProperties> = {
  success: { backgroundColor: '#dcfce7', color: '#166534', border: '1px solid #bbf7d0' },
  warning: { backgroundColor: '#fef9c3', color: '#854d0e', border: '1px solid #fef08a' },
  danger: { backgroundColor: '#fee2e2', color: '#991b1b', border: '1px solid #fecaca' },
  info: { backgroundColor: '#dbeafe', color: '#1e40af', border: '1px solid #bfdbfe' },
  neutral: { backgroundColor: '#f3f4f6', color: '#374151', border: '1px solid #e5e7eb' },
};

function getHealthVariant(status: HealthStatus): BadgeVariant {
  switch (status) {
    case 'healthy': return 'success';
    case 'unhealthy': return 'danger';
    case 'unknown': return 'neutral';
  }
}

function getConnectionVariant(status: ConnectionStatus): BadgeVariant {
  switch (status) {
    case 'connected': return 'success';
    case 'degraded': return 'warning';
    case 'disconnected': return 'danger';
  }
}

function getPlanStatusVariant(status: PlanStatus): BadgeVariant {
  switch (status) {
    case 'pending_approval': return 'warning';
    case 'approved': return 'success';
    case 'rejected': return 'danger';
    case 'pending_forwarding': return 'info';
    case 'forwarding_failed': return 'danger';
    case 'dry_run_passed': return 'success';
    case 'dry_run_failed': return 'danger';
    case 'applied': return 'success';
    case 'rolled_back': return 'warning';
    case 'rollback_failed': return 'danger';
    case 'blocked': return 'danger';
  }
}

function getRunStatusVariant(status: RunStatus): BadgeVariant {
  switch (status) {
    case 'queued': return 'neutral';
    case 'running': return 'info';
    case 'waiting_approval': return 'warning';
    case 'completed': return 'success';
    case 'failed': return 'danger';
    case 'manually_halted': return 'warning';
    case 'unresponsive': return 'danger';
    case 'timed_out': return 'warning';
  }
}

function getRollbackVariant(status: RollbackStatus): BadgeVariant {
  switch (status) {
    case 'pending': return 'neutral';
    case 'in_progress': return 'info';
    case 'completed': return 'success';
    case 'failed': return 'danger';
  }
}

const baseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: '2px 8px',
  borderRadius: '9999px',
  fontSize: '0.75rem',
  fontWeight: 500,
  textTransform: 'capitalize',
  whiteSpace: 'nowrap',
};

type StatusBadgeProps =
  | { type: 'health'; status: HealthStatus }
  | { type: 'connection'; status: ConnectionStatus }
  | { type: 'plan'; status: PlanStatus }
  | { type: 'run'; status: RunStatus }
  | { type: 'rollback'; status: RollbackStatus };

export function StatusBadge(props: StatusBadgeProps) {
  let variant: BadgeVariant;
  switch (props.type) {
    case 'health': variant = getHealthVariant(props.status); break;
    case 'connection': variant = getConnectionVariant(props.status); break;
    case 'plan': variant = getPlanStatusVariant(props.status); break;
    case 'run': variant = getRunStatusVariant(props.status); break;
    case 'rollback': variant = getRollbackVariant(props.status); break;
  }

  const label = props.status.replace(/_/g, ' ');

  return (
    <span style={{ ...baseStyle, ...VARIANT_STYLES[variant] }}>
      {label}
    </span>
  );
}
