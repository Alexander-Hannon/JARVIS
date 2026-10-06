import { useEffect, useState } from 'react';
import QRCode from 'qrcode';
import { api } from '../api/client';
import type { TotpBegin } from '../api/types';
import { createPasskey, passkeysSupported } from '../lib/webauthn';
import { ErrorBox } from './ui';

/** Add a second factor: an authenticator app (QR + 6-digit confirm) or a passkey. Calls onDone with recovery codes
 *  when this was the first factor (they're shown once). */
export function MfaEnrollPanel({ onDone }: { onDone: (recoveryCodes: string[] | null) => void }) {
  const [totp, setTotp] = useState<TotpBegin | null>(null);
  const [svg, setSvg] = useState('');
  const [code, setCode] = useState('');
  const [passkeyLabel, setPasskeyLabel] = useState(() => (/iPhone|iPad|Android/i.test(navigator.userAgent) ? 'Phone passkey' : 'Laptop passkey'));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    if (!totp) return;
    QRCode.toString(totp.otpauth_uri, { type: 'svg', margin: 1, errorCorrectionLevel: 'M' }).then(setSvg, () => setSvg(''));
  }, [totp]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mfa-enroll">
      <section>
        <h4>Authenticator app</h4>
        <p className="hint">Google Authenticator, Microsoft Authenticator, 1Password, Authy… Works from any address you use JARVIS on.</p>
        {!totp ? (
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void run(async () => setTotp(await api.totpBegin('Authenticator app')))}
                  data-testid="totp-begin">
            Set up an authenticator app
          </button>
        ) : (
          <form className="totp-confirm" onSubmit={(e) => { e.preventDefault(); void run(async () => onDone((await api.totpConfirm(totp.id, code.trim())).recovery_codes)); }}>
            {svg ? <div className="qr" aria-label="QR code for your authenticator app" dangerouslySetInnerHTML={{ __html: svg }} /> : null}
            <p className="small muted">
              Scan the code, or enter this key manually: <code className="secret-key" data-testid="totp-secret">{totp.secret.replace(/(.{4})/g, '$1 ').trim()}</code>
            </p>
            <label className="field">
              <span>Then type the 6-digit code it shows</span>
              <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" required
                     pattern="[0-9 ]{6,7}" placeholder="123456" data-testid="totp-code" autoFocus />
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy}>Confirm</button>
          </form>
        )}
      </section>
      {passkeysSupported() && (
        <section>
          <h4>Passkey</h4>
          <p className="hint">Face ID, Touch ID, Windows Hello or a security key. Phishing-resistant; tied to the address you're on now.</p>
          <div className="row">
            <input aria-label="Passkey name" value={passkeyLabel} onChange={(e) => setPasskeyLabel(e.target.value)} />
            <button type="button" className="btn" disabled={busy} onClick={() => void run(async () => {
              const b = await api.passkeyBegin(passkeyLabel);
              const cred = await createPasskey(b.options);
              onDone((await api.passkeyFinish(b.challenge_id, cred)).recovery_codes);
            })}>
              Add a passkey
            </button>
          </div>
        </section>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

export function RecoveryCodes({ codes, onClose }: { codes: string[]; onClose?: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="recovery-codes" role="status" data-testid="recovery-codes">
      <strong>Save these recovery codes now</strong>
      <p className="small">Each works once if you lose your phone or passkey. They won't be shown again.</p>
      <ol>{codes.map((c) => <li key={c}><code>{c}</code></li>)}</ol>
      <div className="row">
        <button type="button" className="btn btn-small" onClick={() => { void navigator.clipboard?.writeText(codes.join('\n')); setCopied(true); }}>
          {copied ? 'Copied' : 'Copy all'}
        </button>
        {onClose && <button type="button" className="btn btn-small btn-primary" onClick={onClose}>I saved them</button>}
      </div>
    </div>
  );
}

/** Full-screen gate for someone whose role requires a second factor and who hasn't set one up yet. */
export function MfaGate({ name, onDone, onSignOut }: { name: string; onDone: () => void; onSignOut: () => void }) {
  const [codes, setCodes] = useState<string[] | null>(null);
  const [enrolled, setEnrolled] = useState(false);
  return (
    <div className="auth-wrap">
      <div className="auth-card wide" data-testid="mfa-gate">
        <div className="brand"><span className="brand-mark" aria-hidden="true" /> JARVIS</div>
        <h1>Protect your account, {name}</h1>
        {!enrolled ? (
          <>
            <p className="muted">Your account needs a second sign-in step. Set one up to continue — it takes a minute.</p>
            <MfaEnrollPanel onDone={(c) => { setEnrolled(true); setCodes(c); if (!c) onDone(); }} />
          </>
        ) : codes ? (
          <RecoveryCodes codes={codes} onClose={onDone} />
        ) : null}
        <button type="button" className="link-btn" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  );
}
