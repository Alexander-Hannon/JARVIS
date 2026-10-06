import { useState } from 'react';
import { api } from '../../api/client';
import type { MfaOverview } from '../../api/types';
import { MfaEnrollPanel, RecoveryCodes } from '../../components/MfaEnroll';
import { Async, Badge, Card, ErrorBox } from '../../components/ui';
import { fmtDateTime, fmtRelative } from '../../lib/format';
import { useAsync } from '../../lib/hooks';
import { useAuth } from '../../state/auth';

const ROLE_LABEL: Record<string, string> = {
  owner: 'Owner', admin: 'Admin', approver: 'Approver', member: 'Member', auditor: 'Auditor (read-only)',
};

export function AccountSection() {
  const { me } = useAuth();
  const mfa = useAsync(() => api.mfa(), []);
  const [name, setName] = useState(me?.display_name ?? '');
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [codes, setCodes] = useState<string[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setErr(null);
    setMsg(null);
    try {
      await fn();
      if (ok) setMsg(ok);
    } catch (e) {
      setErr(e);
    }
  };

  return (
    <>
      <Card title="Your account">
        {me && (
          <p className="muted" data-testid="account-identity">
            Signed in as <strong>{me.username}</strong> · <Badge tone="info">{ROLE_LABEL[me.role] ?? me.role}</Badge>
          </p>
        )}
        <form className="row" onSubmit={(e) => { e.preventDefault(); void act(() => api.patchSettings({ display_name: name }), 'Name saved.'); }}>
          <label className="field grow">
            <span>Name JARVIS uses for you</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
          </label>
          <button type="submit" className="btn">Save</button>
        </form>
        <h4>Change password</h4>
        <form className="stack" onSubmit={(e) => {
          e.preventDefault();
          void act(async () => {
            const r = await api.changePassword(cur, next);
            setCur('');
            setNext('');
            setMsg(`Password changed.${r.signed_out ? ` ${r.signed_out} other device(s) were signed out.` : ''}`);
          });
        }}>
          <label className="field"><span>Current password</span>
            <input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required /></label>
          <label className="field"><span>New password (12+ characters)</span>
            <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} minLength={12} required /></label>
          <button type="submit" className="btn">Change password</button>
        </form>
        {msg && <p className="ok-text" role="status">{msg}</p>}
        <ErrorBox error={err} />
      </Card>

      <Card title="Second sign-in step">
        <Async state={mfa}>
          {(m: MfaOverview) => (
            <div data-testid="mfa-overview">
              <p className="hint">
                {m.required ? 'Required for your account.' : 'Optional for your account — recommended.'}{' '}
                {m.methods.length === 0 ? 'Nothing set up yet.' : `${m.recovery_codes_left} recovery code(s) left.`}
              </p>
              <ul className="device-list">
                {m.methods.map((x) => (
                  <li key={x.id}>
                    <div>
                      <strong>{x.label}</strong> <Badge tone="neutral">{x.kind === 'totp' ? 'Authenticator app' : 'Passkey'}</Badge>
                      <div className="muted small">
                        added {fmtDateTime(x.created_at)}{x.last_used_at ? ` · last used ${fmtRelative(x.last_used_at)}` : ''}
                      </div>
                    </div>
                    <button type="button" className="btn btn-small btn-danger-ghost"
                            onClick={() => void act(async () => { await api.removeMfa(x.id); await mfa.reload(); })}>
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
              {codes && <RecoveryCodes codes={codes} onClose={() => setCodes(null)} />}
              {adding ? (
                <MfaEnrollPanel onDone={(c) => { setAdding(false); setCodes(c); void mfa.reload(); }} />
              ) : (
                <div className="row">
                  <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>Add a method</button>
                  {m.methods.length > 0 && (
                    <button type="button" className="btn" onClick={() => void act(async () => {
                      if (!window.confirm('Make new recovery codes? The old ones stop working.')) return;
                      setCodes((await api.newRecoveryCodes()).recovery_codes);
                      await mfa.reload();
                    })}>
                      New recovery codes
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </Async>
      </Card>
    </>
  );
}
