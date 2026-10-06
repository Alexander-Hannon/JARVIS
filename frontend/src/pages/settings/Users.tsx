import { useState } from 'react';
import { api } from '../../api/client';
import type { AdminUser, Role } from '../../api/types';
import { Async, Badge, Card, ErrorBox } from '../../components/ui';
import { fmtRelative } from '../../lib/format';
import { useAsync } from '../../lib/hooks';
import { useAuth } from '../../state/auth';

const ROLES: { key: Role; label: string; help: string }[] = [
  { key: 'member', label: 'Member', help: 'Uses JARVIS for their own work' },
  { key: 'approver', label: 'Approver', help: 'Member who can co-approve org-wide changes' },
  { key: 'auditor', label: 'Auditor', help: 'Read-only: who did what, when (no content)' },
  { key: 'admin', label: 'Admin', help: 'Manages people, connections, budget and security' },
  { key: 'owner', label: 'Owner', help: 'Everything, including owners and admins' },
];

/** People on this install. Admins see account metadata only — never anyone's conversations, memories or files. */
export function UsersSection() {
  const { me, can } = useAuth();
  const users = useAsync(() => api.users(), []);
  const [username, setUsername] = useState('');
  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('member');
  const [link, setLink] = useState<{ who: string; url: string; hours: number } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const grantable = ROLES.filter((r) => can('users.owner') || !['owner', 'admin'].includes(r.key));
  const editable = (u: AdminUser) => u.id !== me?.id && (can('users.owner') || !['owner', 'admin'].includes(u.role));

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      await users.reload();
    } catch (e) {
      setErr(e);
    }
  };

  return (
    <>
      <Card title="Invite someone">
        <form className="stack" onSubmit={(e) => {
          e.preventDefault();
          void act(async () => {
            const r = await api.inviteUser({ username: username.trim(), display_name: name.trim(), role });
            setLink({ who: r.user.display_name, url: r.invite_url, hours: r.invite_expires_hours });
            setUsername('');
            setName('');
          });
        }}>
          <div className="row">
            <label className="field grow"><span>Name</span><input value={name} onChange={(e) => setName(e.target.value)} required /></label>
            <label className="field grow"><span>Username</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)} required pattern="[a-zA-Z0-9][a-zA-Z0-9._-]{1,31}" /></label>
            <label className="field"><span>Role</span>
              <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
                {grantable.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
              </select>
            </label>
          </div>
          <p className="hint">{ROLES.find((r) => r.key === role)?.help}. They set their own password from a single-use link.</p>
          <button type="submit" className="btn btn-primary">Create invite link</button>
        </form>
        {link && (
          <div className="invite-link" role="status" data-testid="invite-link">
            Send this to {link.who} (works once, for {link.hours} hours):
            <code>{link.url}</code>
            <button type="button" className="btn btn-small" onClick={() => void navigator.clipboard?.writeText(link.url)}>Copy</button>
          </div>
        )}
        <ErrorBox error={err} />
      </Card>
      <Card title="People">
        <Async state={users}>
          {(list: AdminUser[]) => (
            <ul className="device-list" data-testid="user-list">
              {list.map((u) => (
                <li key={u.id}>
                  <div>
                    <strong>{u.display_name}</strong> <span className="muted">@{u.username}</span>{' '}
                    <Badge tone={u.role === 'owner' || u.role === 'admin' ? 'accent' : 'neutral'}>{u.role}</Badge>{' '}
                    {u.status === 'disabled' && <Badge tone="bad">disabled</Badge>}
                    {u.mfa_methods === 0 && u.status === 'active' && <Badge tone="warn">no second step</Badge>}
                    <div className="muted small">
                      {u.last_login_at ? `last signed in ${fmtRelative(u.last_login_at)}` : u.invite_pending_until ? 'invite pending' : 'never signed in'}
                      {` · ${u.active_sessions} device(s)`}
                    </div>
                  </div>
                  {editable(u) && (
                    <div className="row">
                      <select aria-label={`Role for ${u.username}`} value={u.role}
                              onChange={(e) => void act(() => api.updateUser(u.id, { role: e.target.value as Role }))}>
                        {grantable.concat(ROLES.filter((r) => r.key === u.role && !grantable.includes(r)))
                          .map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
                      </select>
                      <button type="button" className="btn btn-small"
                              onClick={() => void act(async () => {
                                const r = await api.reinviteUser(u.id);
                                setLink({ who: u.display_name, url: r.invite_url, hours: r.invite_expires_hours });
                              })} disabled={u.status !== 'active'}>
                        New link
                      </button>
                      {u.mfa_methods > 0 && (
                        <button type="button" className="btn btn-small"
                                onClick={() => window.confirm(`Reset ${u.display_name}'s second step? They'll be signed out and set it up again.`) &&
                                  void act(() => api.resetUserMfa(u.id))}>
                          Reset 2-step
                        </button>
                      )}
                      <button type="button" className={`btn btn-small ${u.status === 'active' ? 'btn-danger-ghost' : ''}`}
                              onClick={() => void act(() => api.updateUser(u.id, { status: u.status === 'active' ? 'disabled' : 'active' }))}>
                        {u.status === 'active' ? 'Disable' : 'Re-enable'}
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Card>
    </>
  );
}
