import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { api, onUnauthorized, setCsrfToken } from './api/client';
import type { AuthStatus } from './api/types';
import { ErrorBox, Loading } from './components/ui';
import { Layout, MorePage } from './components/Layout';
import { AppStateProvider } from './state/app';
import { AuthScreen } from './pages/Auth';
import { TodayPage } from './pages/Today';
import { ConsolePage } from './pages/Console';
import { ActionsPage } from './pages/Actions';
import { ProjectsPage } from './pages/Projects';
import { MemoryPage } from './pages/Memory';
import { MissionsPage } from './pages/Missions';
import { InboxPage } from './pages/Inbox';
import { WorkbenchPage } from './pages/Workbench';
import { ConnectionsPage } from './pages/Connections';
import { HelpPage } from './pages/Help';
import { ActivityPage } from './pages/Activity';
import { SettingsPage } from './pages/Settings';
import { NotificationsPage } from './pages/Notifications';

export function App() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<unknown>(null);

  const check = useCallback(async () => {
    try {
      const s = await api.authStatus();
      setCsrfToken(s.authenticated ? s.csrf_token : null);
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void check();
    onUnauthorized(() => {
      setCsrfToken(null);
      setStatus((s) => (s ? { ...s, authenticated: false } : s));
    });
    return () => onUnauthorized(null);
  }, [check]);

  if (error && !status) {
    return (
      <div className="auth-wrap">
        <div className="auth-card">
          <h1>JARVIS</h1>
          <ErrorBox error={error} onRetry={() => void check()} title="Cannot reach JARVIS" />
        </div>
      </div>
    );
  }
  if (!status) return <div className="auth-wrap"><Loading label="Connecting to JARVIS…" /></div>;
  if (!status.authenticated) {
    return (
      <AuthScreen
        status={status}
        onAuthed={(token) => {
          setCsrfToken(token);
          setStatus({ ...status, authenticated: true, owner_exists: true, csrf_token: token });
        }}
      />
    );
  }

  return (
    <BrowserRouter>
      <AppStateProvider>
        <Layout>
          <Routes>
            <Route path="/" element={<TodayPage />} />
            <Route path="/console" element={<ConsolePage />} />
            <Route path="/actions" element={<ActionsPage />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/briefings" element={<Navigate to="/missions" replace />} />
            <Route path="/missions" element={<MissionsPage />} />
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/workbench" element={<WorkbenchPage />} />
            <Route path="/connections" element={<ConnectionsPage />} />
            <Route path="/help" element={<HelpPage />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/settings" element={<SettingsPage onLoggedOut={() => void check()} />} />
            <Route path="/notifications" element={<NotificationsPage />} />
            <Route path="/more" element={<MorePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </AppStateProvider>
    </BrowserRouter>
  );
}
