interface DemoModeBannerProps {
  active: boolean;
}

const bannerStyle: React.CSSProperties = {
  position: 'sticky',
  top: 0,
  zIndex: 1000,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '8px 16px',
  backgroundColor: '#fbbf24',
  color: '#78350f',
  fontWeight: 600,
  fontSize: '0.8rem',
  letterSpacing: '0.05em',
  textTransform: 'uppercase',
};

export function DemoModeBanner({ active }: DemoModeBannerProps) {
  if (!active) return null;

  return (
    <div style={bannerStyle} role="banner" aria-label="Demo Mode Active">
      Demo Mode Active - No real database connections
    </div>
  );
}
