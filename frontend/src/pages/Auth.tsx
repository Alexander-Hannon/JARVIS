import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { AuthStatus, InviteInfo, MfaChallenge } from '../api/types';
import { ErrorBox } from '../components/ui';
import { getPasskeyAssertion, passkeysSupported } from '../lib/webauthn';

type Mode = 'login' | 'pair' | 'setup' | 'mfa' | 'invite';

/** The invite link is /invite/<token>; the token is read once and kept out of history afterwards. */
export function inviteTokenFromUrl(): string | null {
  const m = window.location.pathname.match(/^\/invite\/([A-Za-z0-9_-]{10,128})$/);
  return m ? m[1] : null;
}

export function AuthScreen({ status, onAuthed, notice }: { status: AuthStatus; onAuthed: (csrf: string) => void; notice?: string }) {
  const [inviteToken] = useState(inviteTokenFromUrl);
  const initial: Mode = inviteToken ? 'invite' : !status.owner_exists ? 'setup' : 'login';
  const [mode, setMode] = useState<Mode>(initial);
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [code, setCode] = useState('');
  const [setupToken, setSetupToken] = useState('');
  const [label, setLabel] = useState(() => (/iPhone|iPad|Android/i.test(navigator.userAgent) ? 'Phone' : 'Browser'));
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null);
  const [mfaMethod, setMfaMethod] = useState<'totp' | 'recovery'>('totp');
  const [invite, setInvite] = useState<InviteInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    if (!inviteToken) return;
    api.inviteInfo(inviteToken).then(setInvite, setErr);
  }, [inviteToken]);

  const finish = (csrf: string) => {
    setPassword('');
    setConfirm('');
    setCode('');
    if (inviteToken) window.history.replaceState(null, '', '/');
    onAuthed(csrf);
  };

  const usePasskey = async () => {
    if (!challenge?.webauthn) return;
    setBusy(true);
    setErr(null);
    try {
      const credential = await getPasskeyAssertion(challenge.webauthn);
      finish((await api.mfaVerify(challenge.challenge_id, 'webauthn', undefined, credential)).csrf_token);
    } catch (x) {
      setErr(x);
      setMode('login');      // the challenge is single-use: sign in again
      setChallenge(null);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      if (mode === 'setup' || mode === 'invite') {
        if (password.length < 12) throw new Error('Use at least 12 characters.');
        if (password !== confirm) throw new Error('Passwords do not match.');
        const r = mode === 'setup'
          ? await api.setup(username.trim(), password, displayName.trim(), status.setup_token_required ? setupToken.trim() : undefined)
          : await api.acceptInvite(inviteToken ?? '', password);
        finish(r.csrf_token);
      } else if (mode === 'login') {
        const r = await api.login(username.trim(), password);
        if ('mfa_required' in r) {
          setChallenge(r);
          setMfaMethod(r.methods.includes('totp') ? 'totp' : 'recovery');
          setMode('mfa');
          setPassword('');
          if (r.methods.includes('webauthn') && passkeysSupported() && !r.methods.includes('totp')) void usePasskey();
        } else finish(r.csrf_token);
      } else if (mode === 'mfa' && challenge) {
        finish((await api.mfaVerify(challenge.challenge_id, mfaMethod, code.trim())).csrf_token);
      } else {
        finish((await api.pair(code.trim(), label.trim() || 'Device')).csrf_token);
      }
    } catch (x) {
      setErr(x);
      if (mode === 'mfa') {          // the challenge is single-use: a wrong code means signing in again
        setMode('login');
        setChallenge(null);
        setCode('');
      }
    } finally {
      setBusy(false);
    }
  };

  const passwordFields = (
    <>
      <label className="field">
        <span>{mode === 'invite' ? 'Choose a password' : 'New password'}</span>
        <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={12} />
      </label>
      <label className="field">
        <span>Confirm password</span>
        <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      </label>
    </>
  );

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit} aria-labelledby="auth-title">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" /> JARVIS
        </div>
        {notice && mode === 'login' && <p className="warn-text" role="status">{notice}</p>}
        {mode === 'setup' && (
          <>
            <h1 id="auth-title">Create the owner account</h1>
            <p className="muted">One-time setup. The owner manages people, connections and security for this JARVIS.</p>
            {status.setup_token_required && (
              <label className="field">
                <span>Setup token</span>
                <input value={setupToken} onChange={(e) => setSetupToken(e.target.value)} required autoComplete="one-time-code"
                       placeholder="XXXXXX-XXXXXX-…" data-testid="setup-token" />
                <small className="muted">Shown in the terminal where <code>jarvis serve</code> runs, or run <code>jarvis setup-token</code> on that machine.</small>
              </label>
            )}
            <label className="field">
              <span>Username</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)} required autoComplete="username" autoFocus
                     pattern="[a-zA-Z0-9][a-zA-Z0-9._-]{1,31}" data-testid="username" />
            </label>
            <label className="field">
              <span>Your name</span>
              <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} autoComplete="name" placeholder="How JARVIS should address you" />
            </label>
            {passwordFields}
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
        {mode === 'invite' && (
          <>
            <h1 id="auth-title">Welcome{invite ? `, ${invite.display_name}` : ''}</h1>
            <p className="muted">
              {invite ? <>Your username is <strong>{invite.username}</strong>. Choose a password to finish setting up your account.</>
                : 'Checking your invite link…'}
            </p>
            {invite && passwordFields}
          </>
        )}
        {mode === 'login' && (
          <>
            <h1 id="auth-title">Sign in</h1>
            <label className="field">
              <span>Username</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)} required autoComplete="username" autoFocus data-testid="username" />
            </label>
            <label className="field">
              <span>Password</span>
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required data-testid="password" />
            </label>
          </>
        )}
        {mode === 'mfa' && challenge && (
          <>
            <h1 id="auth-title">Second step</h1>
            {challenge.methods.includes('webauthn') && passkeysSupported() && (
              <button type="button" className="btn btn-large btn-block" onClick={() => void usePasskey()} disabled={busy} data-testid="use-passkey">
                Use a passkey
              </button>
            )}
            {(challenge.methods.includes('totp') || mfaMethod === 'recovery') && (
              <label className="field">
                <span>{mfaMethod === 'totp' ? '6-digit code from your authenticator app' : 'Recovery code'}</span>
                <input value={code} onChange={(e) => setCode(e.target.value)} required autoFocus data-testid="mfa-code"
                       inputMode={mfaMethod === 'totp' ? 'numeric' : 'text'} autoComplete="one-time-code"
                       placeholder={mfaMethod === 'totp' ? '123456' : 'XXXX-XXXX'} />
              </label>
            )}
            {challenge.methods.includes('recovery') && (
              <button type="button" className="link-btn" onClick={() => { setCode(''); setMfaMethod(mfaMethod === 'totp' ? 'recovery' : 'totp'); }}>
                {mfaMethod === 'totp' ? 'Use a recovery code instead' : 'Use my authenticator app instead'}
              </button>
            )}
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
        {!(mode === 'mfa' && challenge && !challenge.methods.includes('totp') && mfaMethod !== 'recovery') && !(mode === 'invite' && !invite) && (
          <button className="btn btn-primary btn-large btn-block" type="submit" disabled={busy}>
            {busy ? 'Working…' : mode === 'setup' ? 'Create owner account' : mode === 'invite' ? 'Set password and sign in'
              : mode === 'login' ? 'Sign in' : mode === 'mfa' ? 'Verify' : 'Pair device'}
          </button>
        )}
        {(mode === 'login' || mode === 'pair') && (
          <button type="button" className="link-btn" onClick={() => { setErr(null); setMode(mode === 'login' ? 'pair' : 'login'); }}>
            {mode === 'login' ? 'Pair with a code instead' : 'Sign in with password instead'}
          </button>
        )}
        {status.remote_mode && <p className="muted small">Remote mode: connection must be HTTPS.</p>}
      </form>
    </div>
  );
}
