import { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import { DemoModeBanner } from './components';
import { demoApi, getApiToken, setApiToken } from './api/client';
import {
  FleetOverview,
  ActiveRuns,
  EvidenceViewer,
  ApprovalQueue,
  RollbackControls,
  AuditLog,
  ReportsViewer,
  StartTuning,
  TuningSession,
} from './pages';

function App() {
  const [demoActive, setDemoActive] = useState(false);
  const [authenticated, setAuthenticated] = useState(() => Boolean(getApiToken()));
  const [token, setToken] = useState('');

  useEffect(() => {
    demoApi.getStatus().then((status) => {
      setDemoActive(status.active);
    }).catch(() => {
      // Demo API may not be available
    });
  }, []);

  const navLinkStyle = ({ isActive }: { isActive: boolean }): React.CSSProperties => ({
    color: isActive ? '#ffffff' : '#a0aec0',
    textDecoration: 'none',
    marginLeft: '1.5rem',
    fontSize: '0.9rem',
    fontWeight: isActive ? 600 : 400,
    borderBottom: isActive ? '2px solid #3b82f6' : 'none',
    paddingBottom: '2px',
  });

  if (!authenticated) {
    return (
      <main style={{ maxWidth: 460, margin: '12vh auto', padding: '2rem' }}>
        <h1>DBTune control plane</h1>
        <p>Enter your API access token. It is kept only in this browser tab.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          const value = token.trim();
          if (!value) return;
          setApiToken(value);
          setAuthenticated(true);
        }}>
          <input
            aria-label="API access token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            style={{ width: '100%', padding: '0.75rem', boxSizing: 'border-box' }}
          />
          <button type="submit" style={{ marginTop: '1rem', padding: '0.65rem 1rem' }}>
            Sign in
          </button>
        </form>
      </main>
    );
  }

  return (
    <Router future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <DemoModeBanner active={demoActive} />
      <div className="app">
        <header className="app-header">
          <h1 style={{ margin: 0, fontSize: '1.1rem' }}>Autonomous Postgres DBA Agent</h1>
          <nav style={{ display: 'flex', alignItems: 'center' }}>
            <NavLink to="/" style={navLinkStyle} end>Fleet</NavLink>
            <NavLink to="/runs" style={navLinkStyle}>Tuning</NavLink>
            <NavLink to="/plans" style={navLinkStyle}>Approvals</NavLink>
            <NavLink to="/audit" style={navLinkStyle}>Audit</NavLink>
            <button onClick={() => {
              setApiToken('');
              setAuthenticated(false);
            }} style={{ marginLeft: '1.5rem' }}>Sign out</button>
          </nav>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<FleetOverview />} />
            <Route path="/runs" element={<ActiveRuns />} />
            <Route path="/tuning/new" element={<StartTuning />} />
            <Route path="/tuning/:runId" element={<TuningSession />} />
            <Route path="/plans" element={<ApprovalQueue />} />
            <Route path="/evidence" element={<EvidenceViewer />} />
            <Route path="/rollback" element={<RollbackControls />} />
            <Route path="/audit" element={<AuditLog />} />
            <Route path="/reports" element={<ReportsViewer />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;
