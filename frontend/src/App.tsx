import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { api, onMfaNeeded, onUnauthorized, setCsrfToken } from './api/client';
import type { AuthStatus } from './api/types';
import { ErrorBox, Loading } from './components/ui';
import { Layout, MorePage } from './components/Layout';
import { AppStateProvider } from './state/app';
import { AuthScreen, inviteTokenFromUrl } from './pages/Auth';
import { MfaGate } from './components/MfaEnroll';
import { AuthProvider } from './state/auth';
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
  const [notice, setNotice] = useState<string | undefined>(undefined);

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

  const signOut = useCallback(async (why?: string) => {
    try {
      await api.logout();
    } catch {
      /* already signed out */
    }
    setCsrfToken(null);
    setNotice(why);
    await check();
  }, [check]);

  useEffect(() => {
    void check();
    onUnauthorized(() => {
      setCsrfToken(null);
      setStatus((s) => (s ? { ...s, authenticated: false } : s));
    });
    onMfaNeeded(() => void check());
    return () => {
      onUnauthorized(null);
      onMfaNeeded(null);
    };
  }, [check]);

  // A password-only session after the account gained a second factor must sign in again with it.
  useEffect(() => {
    if (status?.authenticated && status.mfa && status.mfa.required && !status.mfa.satisfied && status.mfa.enrolled) {
      void signOut('For security, sign in again with your second step.');
    }
  }, [status, signOut]);

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
  if (!status.authenticated || inviteTokenFromUrl()) {
    return (
      <AuthScreen
        status={status}
        notice={notice}
        onAuthed={(token) => {
          setCsrfToken(token);
          setNotice(undefined);
          void check();
        }}
      />
    );
  }
  if (status.mfa && status.mfa.required && !status.mfa.satisfied) {
    if (status.mfa.enrolled) return <div className="auth-wrap"><Loading label="Signing out…" /></div>;
    return <MfaGate name={status.user?.display_name ?? 'there'} onDone={() => void check()} onSignOut={() => void signOut()} />;
  }

  return (
    <BrowserRouter>
      <AuthProvider me={status.user ?? null} signOut={() => void signOut()}>
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
      </AuthProvider>
    </BrowserRouter>
  );
}
