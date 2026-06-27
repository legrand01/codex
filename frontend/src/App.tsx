import { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import { DemoModeBanner } from './components';
import { demoApi } from './api/client';
import {
  FleetOverview,
  ActiveRuns,
  EvidenceViewer,
  ApprovalQueue,
  RollbackControls,
  AuditLog,
  ReportsViewer,
} from './pages';

function App() {
  const [demoActive, setDemoActive] = useState(false);

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

  return (
    <Router>
      <DemoModeBanner active={demoActive} />
      <div className="app">
        <header className="app-header">
          <h1 style={{ margin: 0, fontSize: '1.1rem' }}>Autonomous Postgres DBA Agent</h1>
          <nav style={{ display: 'flex', alignItems: 'center' }}>
            <NavLink to="/" style={navLinkStyle} end>Fleet</NavLink>
            <NavLink to="/runs" style={navLinkStyle}>Runs</NavLink>
            <NavLink to="/plans" style={navLinkStyle}>Plans</NavLink>
            <NavLink to="/evidence" style={navLinkStyle}>Evidence</NavLink>
            <NavLink to="/rollback" style={navLinkStyle}>Rollback</NavLink>
            <NavLink to="/audit" style={navLinkStyle}>Audit</NavLink>
            <NavLink to="/reports" style={navLinkStyle}>Reports</NavLink>
          </nav>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<FleetOverview />} />
            <Route path="/runs" element={<ActiveRuns />} />
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
