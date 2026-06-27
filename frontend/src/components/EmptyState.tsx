interface EmptyStateProps {
  title: string;
  description?: string;
  action?: React.ReactNode;
}

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '48px 24px',
  textAlign: 'center',
  color: '#6b7280',
};

const titleStyle: React.CSSProperties = {
  fontSize: '1.125rem',
  fontWeight: 600,
  color: '#374151',
  marginBottom: '8px',
};

const descStyle: React.CSSProperties = {
  fontSize: '0.875rem',
  color: '#6b7280',
  marginBottom: '16px',
};

export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div style={containerStyle}>
      <div style={{ fontSize: '3rem', marginBottom: '16px' }}>&#x1f4ed;</div>
      <h3 style={titleStyle}>{title}</h3>
      {description && <p style={descStyle}>{description}</p>}
      {action}
    </div>
  );
}
