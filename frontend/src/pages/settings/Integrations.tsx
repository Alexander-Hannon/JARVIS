import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { GoogleCalendar, Integration, Mailbox } from '../../api/types';
import { AppConnectors } from '../../components/AppConnectors';
import { McpServers } from '../../components/McpServers';
import { TelegramCard } from '../../components/TelegramCard';
import { Async, Badge, Card, Empty, ErrorBox, KV, ReasonButton, SecretForm, StateBadge } from '../../components/ui';
import { fmtDateTime, fmtRelative, humanValue, textOf } from '../../lib/format';
import { useAsync } from '../../lib/hooks';

function IntegrationRow({ i, onChanged }: { i: Integration; onChanged: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const reconnect = async () => {
    setErr(null);
    if (i.provider === 'google' || i.provider === 'google_calendar' || i.provider === 'google_drive') {
      try {
        const purpose = i.provider === 'google_drive' || i.scopes.some((s) => s.includes('drive')) ? 'drive' : 'calendar';
        const r = await api.googleStart(purpose);
        window.location.assign(r.auth_url);
      } catch (e) {
        setErr(e);
      }
    } else {
      document.getElementById(`connect-${i.provider}`)?.scrollIntoView({ block: 'center' });
      (document.querySelector(`#connect-${i.provider} input`) as HTMLInputElement | null)?.focus();
    }
  };
  return (
    <li className="card integration" data-testid={`integration-${i.provider}`}>
      <div className="row gap wrap">
        <strong className="grow">
          {i.provider}
          {i.label ? ` · ${i.label}` : ''}
        </strong>
        <StateBadge state={i.status} />
        {i.live_verified ? <Badge tone="ok">live verified</Badge> : <Badge tone="warn" title="Not yet verified against the live provider">not live-verified</Badge>}
      </div>
      <KV
        items={[
          ['Last sync', i.last_sync_at ? `${fmtRelative(i.last_sync_at)} (${fmtDateTime(i.last_sync_at)})` : 'never'],
          ['Scopes', i.scopes.length ? i.scopes.join(', ') : '—'],
          ['Resources', i.grants.length ? i.grants.map((g) => `${g.resource_name} (${g.access}${g.is_default_target ? ', default write target' : ''})`).join('; ') : '—'],
        ]}
      />
      {i.last_error && <p className="bad-text small">Last error: {i.last_error}</p>}
      <div className="row gap wrap">
        <button type="button" className="btn btn-small" onClick={() => void reconnect()}>
          Reconnect
        </button>
        {confirm ? (
          <span className="confirm">
            <span className="small">Disconnect {i.provider}? Tokens are revoked where the provider supports it.</span>
            <button
              type="button"
              className="btn btn-small btn-danger"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await api.disconnect(i.account_id);
                  onChanged();
                } catch (e) {
                  setErr(e);
                } finally {
                  setBusy(false);
                  setConfirm(false);
                }
              }}
            >
              Disconnect
            </button>
            <button type="button" className="btn btn-small" onClick={() => setConfirm(false)}>
              Cancel
            </button>
          </span>
        ) : (
          <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => setConfirm(true)}>
            Disconnect…
          </button>
        )}
      </div>
      <ErrorBox error={err} />
    </li>
  );
}

function TodoistSection({ integration, onChanged }: { integration: Integration | undefined; onChanged: () => void }) {
  const connected = integration && integration.status !== 'disconnected' && integration.status !== 'not_connected';
  const projects = useAsync(() => (connected ? api.todoistProjects() : Promise.resolve([])), [connected]);
  const current = integration?.grants.find((g) => g.is_default_target)?.resource_id ?? '';
  const [target, setTarget] = useState(current);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => setTarget(current), [current]);
  return (
    <Card title="Todoist" id="connect-todoist">
      <SecretForm
        label="Todoist API token"
        placeholder={connected ? 'Connected — enter a new token to replace' : 'Paste token from Todoist → Settings → Integrations → Developer'}
        submitLabel={connected ? 'Replace token' : 'Connect'}
        onSubmit={async (v) => {
          await api.todoistConnect(v);
          onChanged();
        }}
        hint="The token is encrypted on the server and never shown again."
      />
      {connected ? (
        <>
          <div className="form-row">
            <label htmlFor="todoist-target">Default project for new tasks</label>
            <div className="inline">
              <select id="todoist-target" value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">— choose —</option>
                {(projects.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <ReasonButton
                className="btn"
                reason={!target ? 'Choose a project' : target === current ? 'Already the target' : null}
                onClick={async () => {
                  setErr(null);
                  try {
                    await api.todoistTarget(target);
                    setMsg('Target saved.');
                    onChanged();
                  } catch (e) {
                    setErr(e);
                  }
                }}
              >
                Save target
              </ReasonButton>
            </div>
            <ErrorBox error={projects.error} onRetry={() => void projects.reload()} />
          </div>
          <button
            type="button"
            className="btn btn-small"
            onClick={async () => {
              setErr(null);
              try {
                const r = await api.todoistSync();
                setMsg(`Synced: ${r.pulled} pulled, ${r.reconciled} reconciled.`);
                onChanged();
              } catch (e) {
                setErr(e);
              }
            }}
          >
            Sync now
          </button>
        </>
      ) : (
        <p className="muted small">Project selection is available after connecting.</p>
      )}
      {msg && <p className="ok-text">{msg}</p>}
      <ErrorBox error={err} />
    </Card>
  );
}

function CalendarPicker() {
  const cals = useAsync(() => api.googleCalendars(), []);
  const [read, setRead] = useState<Set<string>>(new Set());
  const [write, setWrite] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => {
    if (!cals.data) return;
    setRead(new Set(cals.data.filter((c) => c.selected_read).map((c) => c.id)));
    setWrite(cals.data.find((c) => c.write_target)?.id ?? null);
  }, [cals.data]);
  const canWrite = (c: GoogleCalendar) => c.accessRole === 'owner' || c.accessRole === 'writer';
  return (
    <Async state={cals} empty="No calendars found for the connected account.">
      {(list) => (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setErr(null);
            try {
              await api.setGoogleCalendars([...read], write);
              setMsg('Calendar selection saved.');
            } catch (x) {
              setErr(x);
            }
          }}
        >
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Calendar</th>
                <th scope="col">Read</th>
                <th scope="col">Write target</th>
              </tr>
            </thead>
            <tbody>
              {list.map((c) => (
                <tr key={c.id}>
                  <td>
                    {c.summary} {c.primary && <Badge tone="info">primary</Badge>} <span className="muted small">{c.accessRole}</span>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Read ${c.summary}`}
                      checked={read.has(c.id)}
                      onChange={(e) => {
                        const n = new Set(read);
                        if (e.target.checked) n.add(c.id);
                        else n.delete(c.id);
                        setRead(n);
                      }}
                    />
                  </td>
                  <td>
                    <input
                      type="radio"
                      name="write-target"
                      aria-label={`Write new events to ${c.summary}`}
                      checked={write === c.id}
                      disabled={!canWrite(c)}
                      title={canWrite(c) ? undefined : 'Read-only calendar'}
                      onChange={() => setWrite(c.id)}
                    />
                  </td>
                </tr>
              ))}
              <tr>
                <td className="muted">No write target (events cannot be created)</td>
                <td />
                <td>
                  <input type="radio" name="write-target" aria-label="No write target" checked={write === null} onChange={() => setWrite(null)} />
                </td>
              </tr>
            </tbody>
          </table>
          <button className="btn btn-primary" type="submit">
            Save calendar selection
          </button>
          {msg && <p className="ok-text">{msg}</p>}
          <ErrorBox error={err} />
        </form>
      )}
    </Async>
  );
}

function DriveRoots() {
  const roots = useAsync(() => api.driveRoots(), []);
  const [ids, setIds] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const list = Array.isArray(roots.data) ? roots.data : [];
  return (
    <div>
      <h4>Drive roots</h4>
      <p className="hint">Only these folders are indexed — never your whole Drive.</p>
      {list.length ? (
        <ul className="plain">
          {list.map((r, i) => (
            <li key={i}>{textOf(r)}</li>
          ))}
        </ul>
      ) : roots.error ? (
        <ErrorBox error={roots.error} onRetry={() => void roots.reload()} />
      ) : (
        <Empty>No Drive folders selected.</Empty>
      )}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          try {
            await api.setDriveRoots(ids.split(/[\s,]+/).filter(Boolean));
            setMsg('Drive roots saved.');
            setIds('');
            void roots.reload();
          } catch (x) {
            setErr(x);
          }
        }}
      >
        <label className="field">
          <span>Folder IDs (replace current list; one per line)</span>
          <textarea rows={2} value={ids} onChange={(e) => setIds(e.target.value)} />
        </label>
        <button className="btn" type="submit" disabled={!ids.trim()}>
          Save Drive roots
        </button>
        {msg && <p className="ok-text">{msg}</p>}
        <ErrorBox error={err} />
      </form>
    </div>
  );
}

function GoogleSection({ integrations }: { integrations: Integration[] }) {
  const setup = useAsync(() => api.googleSetup(), []);
  const [err, setErr] = useState<unknown>(null);
  const hasGoogle = integrations.some((i) => i.provider.startsWith('google'));
  const connect = async (purpose: 'calendar' | 'drive') => {
    setErr(null);
    try {
      const r = await api.googleStart(purpose);
      window.location.assign(r.auth_url);
    } catch (e) {
      setErr(e);
    }
  };
  return (
    <Card title="Google Calendar & Drive" id="connect-google">
      <Async state={setup}>
        {(s) => (
          <>
            {!s.configured && (
              <div className="notice notice-warn">
                <strong>Google OAuth client not configured on the server.</strong>
                <ol>
                  <li>In Google Cloud Console, create a project and enable: {s.required_apis.join(', ') || 'Calendar API, Drive API'}.</li>
                  <li>Create an OAuth client (Web application) with redirect URI <code>{s.redirect_uri}</code>.</li>
                  <li>Put the client ID/secret in the JARVIS backend configuration and restart it.</li>
                </ol>
              </div>
            )}
            <KV
              items={[
                [
                  'Redirect URIs to register',
                  <span key="r">
                    {(s.redirect_uris ?? [s.redirect_uri]).map((u) => (
                      <code key={u} style={{ display: 'block' }}>
                        {u}
                      </code>
                    ))}
                  </span>,
                ],
                ['Calendar scopes', s.scopes.calendar.join(', ') || '—'],
                ['Drive scopes', s.scopes.drive.join(', ') || '—'],
              ]}
            />
            <div className="row gap wrap">
              <ReasonButton className="btn btn-primary" reason={s.configured ? null : 'Configure the OAuth client first'} onClick={() => void connect('calendar')}>
                Connect Google Calendar
              </ReasonButton>
              <ReasonButton className="btn" reason={s.configured ? null : 'Configure the OAuth client first'} onClick={() => void connect('drive')}>
                Connect Google Drive
              </ReasonButton>
            </div>
          </>
        )}
      </Async>
      <ErrorBox error={err} />
      {hasGoogle ? (
        <>
          <h4>Calendars</h4>
          <CalendarPicker />
          <DriveRoots />
        </>
      ) : (
        <p className="muted small">Calendar selection and Drive roots appear after connecting.</p>
      )}
    </Card>
  );
}

function MailboxRow({
  label,
  mailbox,
  integration,
  connected,
  address,
  onChanged,
}: {
  label: string;
  mailbox: Mailbox;
  integration: Integration | undefined;
  connected: boolean;
  address: string | null;
  onChanged: () => void;
}) {
  const [err, setErr] = useState<unknown>(null);
  const sending = !!integration?.grants.some((g) => g.resource_type === 'mail_send' && g.access === 'write');
  const toggle = async () => {
    setErr(null);
    if (!sending && !window.confirm(`Allow JARVIS to send from ${label}? Every send still needs your explicit approval, and rules can never send automatically.`)) return;
    try {
      await api.setMailSending(mailbox, !sending);
      onChanged();
    } catch (e) {
      setErr(e);
    }
  };
  return (
    <li className="mailbox-row">
      <div>
        <strong>{label}</strong>{' '}
        <Badge tone={connected ? 'ok' : 'neutral'}>{connected ? 'connected' : 'not connected'}</Badge>
        {address && <span className="muted small"> {address}</span>}
      </div>
      {connected && (
        <div className="row gap wrap small">
          <span className="muted">Reads and drafts: on.</span>
          <span>Sending: {sending ? <strong>on (approval per message)</strong> : 'off'}</span>
          <button type="button" className="btn btn-small" onClick={() => void toggle()}>
            {sending ? 'Turn sending off' : 'Allow sending…'}
          </button>
        </div>
      )}
      <ErrorBox error={err} />
    </li>
  );
}

function EmailSection({ integrations, onChanged }: { integrations: Integration[]; onChanged: () => void }) {
  const google = useAsync(() => api.googleSetup(), []);
  const ms = useAsync(() => api.microsoftSetup(), []);
  const inbox = useAsync(() => api.inbox({ limit: 1 }), []);
  const [err, setErr] = useState<unknown>(null);
  const go = async (fn: () => Promise<{ auth_url: string }>) => {
    setErr(null);
    try {
      window.location.assign((await fn()).auth_url);
    } catch (e) {
      setErr(e);
    }
  };
  const boxes = inbox.data?.mailboxes;
  return (
    <Card title="Email (Gmail & Outlook)" id="connect-email">
      <p className="muted small">
        JARVIS can triage your inbox, read threads you point it at, and save drafts in your own mailbox. Email text is
        treated as untrusted. Recipients must come from your own words or the thread you asked to reply to.
      </p>
      <ul className="stack plain">
        <MailboxRow
          label="Gmail"
          mailbox="gmail"
          integration={integrations.find((i) => i.provider === 'gmail')}
          connected={boxes?.gmail?.status === 'connected'}
          address={boxes?.gmail?.address ?? null}
          onChanged={() => {
            onChanged();
            void inbox.reload();
          }}
        />
        <MailboxRow
          label="Outlook"
          mailbox="outlook"
          integration={integrations.find((i) => i.provider === 'microsoft')}
          connected={boxes?.outlook?.status === 'connected'}
          address={boxes?.outlook?.address ?? null}
          onChanged={() => {
            onChanged();
            void inbox.reload();
          }}
        />
      </ul>
      <div className="row gap wrap">
        <ReasonButton
          className="btn btn-primary"
          reason={google.data?.configured ? null : 'Configure the Google OAuth client first (see Google section)'}
          onClick={() => void go(() => api.googleStart('gmail'))}
        >
          Connect Gmail
        </ReasonButton>
        <ReasonButton
          className="btn"
          reason={ms.data?.configured ? null : 'Configure the Microsoft app registration first (steps below)'}
          onClick={() => void go(() => api.microsoftStart(false))}
        >
          Connect Outlook
        </ReasonButton>
        {boxes?.outlook?.status === 'connected' && (
          <ReasonButton className="btn" reason={ms.data?.configured ? null : 'Not configured'} onClick={() => void go(() => api.microsoftStart(true))}>
            Reconnect Outlook with sending allowed
          </ReasonButton>
        )}
      </div>
      {ms.data && !ms.data.configured && Array.isArray(ms.data.steps) && (
        <details className="small">
          <summary>Set up Outlook (Microsoft app registration)</summary>
          <ol>
            {ms.data.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </details>
      )}
      {google.data?.scopes?.gmail && <p className="muted small">Gmail scopes: {google.data.scopes.gmail.join(', ')}</p>}
      <ErrorBox error={err} />
    </Card>
  );
}

function IcsSection({ integration, onChanged }: { integration: Integration | undefined; onChanged: () => void }) {
  const deadlines = useAsync(() => api.deadlines(), []);
  const mappings = useAsync(() => api.courseMappings(), []);
  const projects = useAsync(() => api.projects(), []);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [course, setCourse] = useState('');
  const [project, setProject] = useState('');
  return (
    <Card title="Course deadline feed (ICS)" id="connect-ics">
      <SecretForm
        label="ICS feed URL"
        placeholder={integration ? 'Connected — enter a new URL to replace' : 'https://…/calendar.ics'}
        submitLabel={integration ? 'Replace URL' : 'Connect feed'}
        onSubmit={async (v) => {
          await api.icsConnect(v);
          onChanged();
        }}
        hint="Feed URLs often contain a private token; it is stored encrypted and never echoed back."
      />
      <button
        type="button"
        className="btn btn-small"
        disabled={!integration}
        title={integration ? undefined : 'Connect a feed first'}
        onClick={async () => {
          setErr(null);
          try {
            const r = await api.icsRefresh();
            setMsg(`Refreshed: ${r.items} items, ${r.changed} changed (${fmtDateTime(r.last_refresh)}).`);
            void deadlines.reload();
          } catch (e) {
            setErr(e);
          }
        }}
      >
        Refresh now
      </button>
      {msg && <p className="ok-text">{msg}</p>}
      <ErrorBox error={err} />
      <h4>Upcoming deadlines</h4>
      <Async state={deadlines} empty="No deadlines imported.">
        {(list) => (
          <ul className="list">
            {list.slice(0, 20).map((d, i) => (
              <li key={d.id ?? i} className="list-row">
                <span>{d.title ?? textOf(d)}</span>
                <span className="muted small">
                  {d.course ? `${d.course} · ` : ''}
                  {d.due_at ? humanValue('due_at', d.due_at) : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Async>
      <h4>Course → project mappings</h4>
      <Async state={mappings} empty="No mappings yet.">
        {(list) => (
          <ul className="plain">
            {list.map((m, i) => (
              <li key={m.id ?? i}>
                {m.course ?? textOf(m)} → {projects.data?.find((p) => p.id === m.project_id)?.name ?? m.project_id ?? '—'}
              </li>
            ))}
          </ul>
        )}
      </Async>
      <form
        className="inline wrap"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          try {
            await api.addCourseMapping({ course: course.trim(), project_id: project });
            setCourse('');
            void mappings.reload();
          } catch (x) {
            setErr(x);
          }
        }}
      >
        <input aria-label="Course code" placeholder="Course (e.g. BIO 201)" value={course} onChange={(e) => setCourse(e.target.value)} />
        <select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">— project —</option>
          {(projects.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button className="btn" type="submit" disabled={!course.trim() || !project}>
          Add mapping
        </button>
      </form>
    </Card>
  );
}

function GithubSection({ onChanged }: { onChanged: () => void }) {
  const status = useAsync(() => api.githubStatus(), []);
  const [token, setToken] = useState('');
  const [repos, setRepos] = useState('');
  const [err, setErr] = useState<unknown>(null);
  const [ok, setOk] = useState(false);
  const st = status.data as { connected?: boolean; repos?: string[]; error?: string } | undefined;
  return (
    <Card title="GitHub" id="connect-github">
      {st && (
        <p className="small">
          {st.connected ? 'Connected' : 'Not connected'}
          {st.repos?.length ? ` · repos: ${st.repos.join(', ')}` : ''}
          {st.error ? <span className="bad-text"> · {st.error}</span> : null}
        </p>
      )}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          setOk(false);
          try {
            await api.githubConnect(token.trim(), repos.split(/[\s,]+/).filter(Boolean));
            setToken('');
            setOk(true);
            onChanged();
            void status.reload();
          } catch (x) {
            setErr(x);
          }
        }}
      >
        <label className="field">
          <span>Fine-grained token (read-only recommended)</span>
          <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </label>
        <label className="field">
          <span>Repositories (owner/name, comma or newline separated)</span>
          <textarea rows={2} value={repos} onChange={(e) => setRepos(e.target.value)} />
        </label>
        <button className="btn btn-primary" type="submit" disabled={!token.trim() || !repos.trim()}>
          Save GitHub access
        </button>
        {ok && <p className="ok-text">Saved. The token will not be shown again.</p>}
        <ErrorBox error={err} />
      </form>
    </Card>
  );
}

export function IntegrationsSection() {
  const integrations = useAsync(() => api.integrations(), []);
  const list = integrations.data ?? [];
  const reload = () => void integrations.reload();
  const find = (p: string) => list.find((i) => i.provider === p);
  return (
    <div className="stack">
      <Card title="Connected accounts">
        <Async state={integrations} empty="No integrations connected yet. Connect sources below.">
          {(l) => (
            <ul className="stack plain">
              {l.map((i) => (
                <IntegrationRow key={i.account_id} i={i} onChanged={reload} />
              ))}
            </ul>
          )}
        </Async>
      </Card>
      <TodoistSection integration={find('todoist')} onChanged={reload} />
      <GoogleSection integrations={list} />
      <EmailSection integrations={list} onChanged={reload} />
      <AppConnectors />
      <McpServers />
      <TelegramCard />
      <IcsSection integration={find('ics')} onChanged={reload} />
      <GithubSection onChanged={reload} />
      <Card title="Anthropic (reasoning model)" id="connect-anthropic">
        <SecretForm
          label="Anthropic API key"
          placeholder={find('anthropic') ? 'Configured — enter a new key to replace' : 'sk-ant-…'}
          submitLabel="Save key"
          onSubmit={async (v) => {
            await api.anthropicKey(v);
            reload();
          }}
          hint="Stored encrypted server-side; never sent to the browser again. Prompts and selected excerpts sent to Anthropic leave this machine."
        />
      </Card>
      {integrations.error && list.length > 0 ? <p className="bad-text small">{errorMessage(integrations.error)}</p> : null}
    </div>
  );
}
