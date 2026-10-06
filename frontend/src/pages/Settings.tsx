import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { AccountSection } from './settings/Account';
import { IntegrationsSection } from './settings/Integrations';
import { UsersSection } from './settings/Users';
import { useAuth } from '../state/auth';
import { LocalOpsSection, PrivacySection, SecuritySection, VoiceSection } from './settings/Personal';
import { BackupsSection, BudgetSection, DoctorSection, NotificationSettingsSection, SchedulesSection } from './settings/System';

// `perm`: who sees the section (the server enforces the same permissions on every call).
const ALL_SECTIONS = [
  { key: 'integrations', label: 'Integrations', perm: 'use' },
  { key: 'account', label: 'Account', perm: null },
  { key: 'voice', label: 'Voice', perm: 'use' },
  { key: 'schedules', label: 'Schedules', perm: 'use' },
  { key: 'notifications', label: 'Notifications', perm: 'use' },
  { key: 'budget', label: 'Budget', perm: 'org.settings' },
  { key: 'privacy', label: 'Privacy', perm: 'use' },
  { key: 'local', label: 'Local files', perm: 'use' },
  { key: 'users', label: 'People', perm: 'users.manage' },
  { key: 'backups', label: 'Backups', perm: 'org.backups' },
  { key: 'doctor', label: 'Diagnostics', perm: 'org.doctor' },
  { key: 'security', label: 'Security', perm: null },
] as const;
type Key = (typeof ALL_SECTIONS)[number]['key'];

const CONNECT_ERRORS: Record<string, string> = {
  bad_state: 'That Google sign-in link expired or was already used. Click Connect again.',
  wrong_browser: 'Finish the Google connection in the same browser where you clicked Connect.',
  denied: 'Google access was not granted (cancelled or blocked). Click Connect to try again.',
  token_exchange_failed: 'Google rejected the sign-in code. Check the client ID/secret in .env, restart Jarvis, and try again.',
};

export function SettingsPage({ onLoggedOut }: { onLoggedOut: () => void }) {
  const loc = useLocation();
  const nav = useNavigate();
  const { can } = useAuth();
  const SECTIONS = ALL_SECTIONS.filter((x) => x.perm === null || can(x.perm));
  const fromHash = (loc.hash.slice(1) || SECTIONS[0].key) as Key;
  const [section, setSection] = useState<Key>(SECTIONS.some((s) => s.key === fromHash) ? fromHash : SECTIONS[0].key);
  useEffect(() => {
    if (SECTIONS.some((s) => s.key === fromHash)) setSection(fromHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromHash]);
  // Result of an OAuth round-trip (?connected=google / ?connect_error=code), shown once.
  const [oauthResult] = useState(() => {
    const q = new URLSearchParams(loc.search);
    const ok = q.get('connected');
    const err = q.get('connect_error');
    return ok ? { ok: true, text: `${ok === 'google' ? 'Google' : ok} connected. Now choose which calendars to read and the one calendar Jarvis may write to.` }
      : err ? { ok: false, text: CONNECT_ERRORS[err] ?? `Connection failed (${err}). Try Connect again.` } : null;
  });
  useEffect(() => {
    if (loc.search) nav({ search: '', hash: 'integrations' }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const go = (k: Key) => {
    setSection(k);
    nav({ hash: k }, { replace: true });
  };

  return (
    <div className="page settings">
      <header className="page-head">
        <h1>Settings</h1>
        <label className="settings-select">
          <span className="sr-only">Settings section</span>
          <select value={section} onChange={(e) => go(e.target.value as Key)}>
            {SECTIONS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </header>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SECTIONS.map((s) => (
            <button key={s.key} type="button" className={`settings-link ${section === s.key ? 'active' : ''}`} aria-current={section === s.key ? 'page' : undefined} onClick={() => go(s.key)}>
              {s.label}
            </button>
          ))}
        </nav>
        <div className="settings-body">
          {oauthResult && (
            <p role={oauthResult.ok ? 'status' : 'alert'} className={oauthResult.ok ? 'ok-text' : 'bad-text'}>
              {oauthResult.text}
            </p>
          )}
          {section === 'integrations' && <IntegrationsSection />}
          {section === 'account' && <AccountSection />}
          {section === 'users' && <UsersSection />}
          {section === 'voice' && <VoiceSection />}
          {section === 'schedules' && <SchedulesSection />}
          {section === 'notifications' && <NotificationSettingsSection />}
          {section === 'budget' && <BudgetSection />}
          {section === 'privacy' && <PrivacySection />}
          {section === 'local' && <LocalOpsSection />}
          {section === 'backups' && <BackupsSection />}
          {section === 'doctor' && <DoctorSection />}
          {section === 'security' && <SecuritySection onLoggedOut={onLoggedOut} />}
        </div>
      </div>
    </div>
  );
}
