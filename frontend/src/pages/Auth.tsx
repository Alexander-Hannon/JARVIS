import { useState } from 'react';
import { api } from '../api/client';
import type { AuthStatus } from '../api/types';
import { ErrorBox } from '../components/ui';

type Mode = 'login' | 'pair' | 'setup';

export function AuthScreen({ status, onAuthed }: { status: AuthStatus; onAuthed: (csrf: string) => void }) {
  const initial: Mode = !status.owner_exists && status.setup_allowed ? 'setup' : 'login';
  const [mode, setMode] = useState<Mode>(initial);
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [code, setCode] = useState('');
  const [label, setLabel] = useState(() => (/iPhone|iPad|Android/i.test(navigator.userAgent) ? 'Phone' : 'Browser'));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      let r;
      if (mode === 'setup') {
        if (password.length < 12) throw new Error('Use at least 12 characters.');
        if (password !== confirm) throw new Error('Passwords do not match.');
        r = await api.setup(password);
      } else if (mode === 'login') r = await api.login(password);
      else r = await api.pair(code.trim(), label.trim() || 'Device');
      setPassword('');
      setConfirm('');
      onAuthed(r.csrf_token);
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  };

  const noOwner = !status.owner_exists && !status.setup_allowed;

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit} aria-labelledby="auth-title">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" /> JARVIS
        </div>
        {mode === 'setup' && (
          <>
            <h1 id="auth-title">Set up your owner password</h1>
            <p className="muted">One-time local setup. This password protects commands, approvals, audio and files.</p>
            <label className="field">
              <span>New password</span>
              <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={12} autoFocus />
            </label>
            <label className="field">
              <span>Confirm password</span>
              <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
            </label>
            <div className="privacy-note">
              <strong>Where your data goes</strong>
              <ul>
                <li>Voice is transcribed locally on this machine; audio is deleted after processing unless you opt in to retention.</li>
                <li>Prompts, selected document excerpts and images sent to Anthropic for reasoning <em>do</em> leave this machine.</li>
                <li>No telemetry or third-party tracking.</li>
              </ul>
            </div>
          </>
        )}
        {mode === 'login' && (
          <>
            <h1 id="auth-title">Sign in</h1>
            {noOwner && <p className="warn-text">No owner account exists yet. Open JARVIS on the host machine (localhost) to run first-time setup.</p>}
            <label className="field">
              <span>Password</span>
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus data-testid="password" />
            </label>
          </>
        )}
        {mode === 'pair' && (
          <>
            <h1 id="auth-title">Pair this device</h1>
            <p className="muted">On a signed-in device, open Settings → Security → Create pairing code, then enter it here.</p>
            <label className="field">
              <span>Pairing code</span>
              <input value={code} onChange={(e) => setCode(e.target.value)} required autoFocus inputMode="text" autoComplete="one-time-code" />
            </label>
            <label className="field">
              <span>Device label</span>
              <input value={label} onChange={(e) => setLabel(e.target.value)} />
            </label>
          </>
        )}
        <ErrorBox error={err} />
        <button className="btn btn-primary btn-large btn-block" type="submit" disabled={busy}>
          {busy ? 'Working…' : mode === 'setup' ? 'Create owner account' : mode === 'login' ? 'Sign in' : 'Pair device'}
        </button>
        {mode !== 'setup' && (
          <button type="button" className="link-btn" onClick={() => { setErr(null); setMode(mode === 'login' ? 'pair' : 'login'); }}>
            {mode === 'login' ? 'Pair with a code instead' : 'Sign in with password instead'}
          </button>
        )}
        {status.remote_mode && <p className="muted small">Remote mode: connection must be HTTPS.</p>}
      </form>
    </div>
  );
}
