import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { IntegrationsSection } from './settings/Integrations';
import { LocalOpsSection, PrivacySection, SecuritySection, VoiceSection } from './settings/Personal';
import { BackupsSection, BudgetSection, DoctorSection, NotificationSettingsSection, SchedulesSection } from './settings/System';

const SECTIONS = [
  { key: 'integrations', label: 'Integrations' },
  { key: 'voice', label: 'Voice' },
  { key: 'schedules', label: 'Schedules' },
  { key: 'notifications', label: 'Notifications' },
  { key: 'budget', label: 'Budget' },
  { key: 'privacy', label: 'Privacy' },
  { key: 'local', label: 'Local files' },
  { key: 'backups', label: 'Backups' },
  { key: 'doctor', label: 'Diagnostics' },
  { key: 'security', label: 'Security' },
] as const;
type Key = (typeof SECTIONS)[number]['key'];

export function SettingsPage({ onLoggedOut }: { onLoggedOut: () => void }) {
  const loc = useLocation();
  const nav = useNavigate();
  const fromHash = (loc.hash.slice(1) || 'integrations') as Key;
  const [section, setSection] = useState<Key>(SECTIONS.some((s) => s.key === fromHash) ? fromHash : 'integrations');
  useEffect(() => {
    if (SECTIONS.some((s) => s.key === fromHash)) setSection(fromHash);
  }, [fromHash]);
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
          {section === 'integrations' && <IntegrationsSection />}
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
