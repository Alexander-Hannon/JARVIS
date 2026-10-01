import { useEffect, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import { NavLink, Link } from 'react-router-dom';
import { useControl, useNotifications, useSpeech } from '../state/app';
import { BellIcon, PauseIcon, PlayIcon } from './icons';
import { CommandPalette } from './CommandPalette';

export const NAV = [
  { to: '/', label: 'Today', icon: '◐', mobile: true },
  { to: '/console', label: 'Command', icon: '◉', mobile: true },
  { to: '/inbox', label: 'Inbox', icon: '✉', mobile: true },
  { to: '/actions', label: 'Actions', icon: '✓', mobile: true },
  { to: '/missions', label: 'Missions', icon: '◎', mobile: false },
  { to: '/workbench', label: 'Workbench', icon: '⌘', mobile: false },
  { to: '/memory', label: 'Memory', icon: '◈', mobile: false },
  { to: '/projects', label: 'Projects', icon: '▦', mobile: false },
  { to: '/connections', label: 'Connections', icon: '⇄', mobile: false },
  { to: '/activity', label: 'Activity', icon: '≋', mobile: false },
  { to: '/settings', label: 'Settings', icon: '⚙', mobile: false },
  { to: '/help', label: 'Help', icon: '?', mobile: false },
] as const;

/** Assistant name from the server's brand setting (one setting renames the product). */
function useBrand(): string {
  const [name, setName] = useState('JARVIS');
  useEffect(() => {
    api.brain().then((b) => b.assistant_name && setName(b.assistant_name), () => undefined);
  }, []);
  return name;
}

function PauseControl() {
  const { control, busy, pause, resume, error } = useControl();
  const [confirmResume, setConfirmResume] = useState(false);
  const paused = !!control?.actions_paused;
  const inflight = control?.in_flight ?? [];
  if (!control) {
    return (
      <button type="button" className="btn pause-btn" disabled title={error ?? 'Loading control state'}>
        {error ? 'Control unavailable' : 'Pause actions'}
      </button>
    );
  }
  if (paused) {
    return confirmResume ? (
      <div className="resume-confirm" role="group" aria-label="Confirm resume">
        <span className="small">Resume? Earlier approvals will NOT be replayed.</span>
        <button type="button" className="btn btn-small btn-primary" disabled={busy} onClick={() => void resume().then(() => setConfirmResume(false))}>
          Resume
        </button>
        <button type="button" className="btn btn-small" onClick={() => setConfirmResume(false)}>
          Keep paused
        </button>
      </div>
    ) : (
      <button type="button" className="btn pause-btn paused" aria-pressed="true" data-testid="pause-toggle" onClick={() => setConfirmResume(true)} disabled={busy}>
        <PlayIcon /> <span className="btn-label">Actions paused{inflight.length ? ` · ${inflight.length} in flight` : ''}</span>
      </button>
    );
  }
  return (
    <button type="button" className="btn pause-btn" aria-pressed="false" data-testid="pause-toggle" onClick={() => void pause('Paused from header')} disabled={busy}>
      <PauseIcon /> <span className="btn-label">Pause actions{inflight.length ? ` · ${inflight.length} in flight` : ''}</span>
    </button>
  );
}

function PausedBanner() {
  const { control } = useControl();
  if (!control?.actions_paused) return null;
  return (
    <div className="paused-banner" role="status" data-testid="paused-banner">
      <strong>All actions paused</strong>
      <span>
        No new writes will run{control.reason ? ` (${control.reason})` : ''}. Paused {control.paused_at ? new Date(control.paused_at).toLocaleTimeString() : ''}.
      </span>
      {control.in_flight.length > 0 && (
        <div>
          Already in flight (may have reached the provider):
          <ul>
            {control.in_flight.map((f) => (
              <li key={f.action_id}>
                {f.summary} — <em>{f.state}</em>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Bell() {
  const { unread } = useNotifications();
  return (
    <Link to="/notifications" className="bell" aria-label={`Notifications: ${unread} unread`} data-testid="bell">
      <BellIcon />
      {unread > 0 && <span className="bell-count">{unread > 99 ? '99+' : unread}</span>}
    </Link>
  );
}

function StopSpeaking() {
  const { speaking, stop } = useSpeech();
  if (!speaking) return null;
  return (
    <button type="button" className="btn btn-small stop-speak" onClick={stop} aria-label="Stop speaking">
      <span aria-hidden="true" className="speak-wave">
        <i />
        <i />
        <i />
      </span>
      <span className="btn-label">Stop speaking</span>
    </button>
  );
}

/** HUD readout: live clock + REAL worker heartbeat (never a decorative "all systems nominal"). */
function HudStatus() {
  const [now, setNow] = useState(() => new Date());
  const [worker, setWorker] = useState<boolean | null>(null);
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    const poll = () => api.workerStatus().then((w) => setWorker(!!w.alive), () => setWorker(false));
    poll();
    const w = window.setInterval(poll, 30000);
    return () => {
      window.clearInterval(t);
      window.clearInterval(w);
    };
  }, []);
  return (
    <div className="hud-status" aria-label="System status">
      <span className="hud-clock">{now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
      <span className={`hud-chip ${worker ? 'on' : worker === false ? 'off' : ''}`} title="Background worker heartbeat">
        <i aria-hidden="true" /> {worker === null ? 'CHECKING' : worker ? 'WORKER ONLINE' : 'WORKER OFFLINE'}
      </span>
    </div>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const brand = useBrand();
  return (
    <div className="shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          {brand}
        </div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
              <span className="nav-icon" aria-hidden="true">
                {n.icon}
              </span>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <div className="brand brand-mobile">
            <span className="brand-mark" aria-hidden="true" />
            {brand}
          </div>
          <HudStatus />
          <CommandPalette />
          <div className="topbar-right">
            <StopSpeaking />
            <PauseControl />
            <Bell />
          </div>
        </header>
        <PausedBanner />
        <main id="main" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
      <nav className="bottom-nav" aria-label="Primary mobile">
        {NAV.filter((n) => n.mobile).map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === '/'} className={({ isActive }) => `bn-link ${isActive ? 'active' : ''}`}>
            <span className="nav-icon" aria-hidden="true">
              {n.icon}
            </span>
            <span>{n.label}</span>
          </NavLink>
        ))}
        <NavLink to="/more" className={({ isActive }) => `bn-link ${isActive ? 'active' : ''}`}>
          <span className="nav-icon" aria-hidden="true">
            ⋯
          </span>
          <span>More</span>
        </NavLink>
      </nav>
    </div>
  );
}

export function MorePage() {
  return (
    <div className="page">
      <h1>More</h1>
      <ul className="more-list">
        {NAV.filter((n) => !n.mobile).map((n) => (
          <li key={n.to}>
            <Link to={n.to} className="more-link">
              <span aria-hidden="true">{n.icon}</span> {n.label}
            </Link>
          </li>
        ))}
        <li>
          <Link to="/notifications" className="more-link">
            <BellIcon /> Notifications
          </Link>
        </li>
      </ul>
    </div>
  );
}
