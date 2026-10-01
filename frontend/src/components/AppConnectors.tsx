import { useState } from 'react';
import { api } from '../api/client';
import type { Connector, ConnectorResources } from '../api/types';
import { useAsync } from '../lib/hooks';
import { Async, Badge, Card, ErrorBox, ReasonButton } from './ui';

/* Every V4 app integration rendered from the backend connector catalog: what the card says JARVIS can read/write is
   exactly what the server registers. */

const FAMILY: Record<string, string> = {
  notes: 'Notes & docs',
  messaging: 'Messaging',
  school: 'School',
  life: 'Life',
  home: 'Home',
  travel: 'Travel',
  finance: 'Finance',
};

function TokenForm({ c, onDone }: { c: Connector; onDone: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const ready = c.fields.every((f) => (values[f.key] ?? '').trim());
  return (
    <form
      className="connector-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        try {
          await api.connectConnector(c.id, Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim()])));
          setValues({});
          onDone();
        } catch (x) {
          setErr(x);
        } finally {
          setBusy(false);
        }
      }}
    >
      {c.fields.map((f) => (
        <label key={f.key} className="field">
          <span>{f.label}</span>
          <input
            type={f.secret ? 'password' : 'text'}
            autoComplete="off"
            placeholder={f.placeholder}
            value={values[f.key] ?? ''}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
          />
        </label>
      ))}
      <button type="submit" className="btn btn-primary" disabled={!ready || busy}>
        {busy ? 'Checking…' : c.status === 'connected' ? 'Replace credentials' : 'Connect'}
      </button>
      <p className="muted small">Stored encrypted on the JARVIS host and never shown again.</p>
      <ErrorBox error={err} />
    </form>
  );
}

function ResourcePicker({ c }: { c: Connector }) {
  const res = useAsync(() => api.connectorResources(c.id), [c.id]);
  const [sel, setSel] = useState<{ read: Set<string>; write: Set<string> } | null>(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const current = (r: ConnectorResources) =>
    sel ?? {
      read: new Set(r.items.filter((i) => i.read).map((i) => i.id)),
      write: new Set(r.items.filter((i) => i.write).map((i) => i.id)),
    };
  return (
    <Async state={res}>
      {(r) => {
        const s = current(r);
        const flip = (acc: 'read' | 'write', id: string) => {
          const next = { read: new Set(s.read), write: new Set(s.write) };
          if (next[acc].has(id)) next[acc].delete(id);
          else next[acc].add(id);
          if (acc === 'read' && !next.read.has(id)) next.write.delete(id); // can't control what JARVIS can't see
          setSel(next);
          setSaved(false);
        };
        return (
          <div className="resource-picker">
            <h4>{r.label}</h4>
            {r.items.length === 0 ? (
              <p className="muted small">Nothing to pick yet (for Notion: share pages with the integration first).</p>
            ) : (
              <table className="pick-table">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    {r.access.map((a) => (
                      <th key={a} scope="col">
                        {a === 'read' ? 'See' : r.access.includes('read') ? 'Control' : 'Allow'}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.items.map((i) => (
                    <tr key={i.id}>
                      <th scope="row">
                        {i.name} {i.detail && <span className="muted small">{i.detail}</span>}
                      </th>
                      {r.access.map((a) => (
                        <td key={a}>
                          <input
                            type="checkbox"
                            aria-label={`${a} ${i.name}`}
                            checked={s[a].has(i.id)}
                            disabled={a === 'write' && r.access.includes('read') && !s.read.has(i.id)}
                            onChange={() => flip(a, i.id)}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="row gap">
              <button
                type="button"
                className="btn btn-small btn-primary"
                disabled={!sel}
                onClick={async () => {
                  setErr(null);
                  try {
                    await api.setConnectorResources(c.id, { read: [...s.read], write: [...s.write] });
                    setSel(null);
                    setSaved(true);
                    void res.reload();
                  } catch (x) {
                    setErr(x);
                  }
                }}
              >
                Save choices
              </button>
              {saved && <span className="ok-text small">Saved.</span>}
            </div>
            <ErrorBox error={err} />
          </div>
        );
      }}
    </Async>
  );
}

function ConnectorCard({ c, onChanged }: { c: Connector; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const connected = c.status === 'connected';
  return (
    <li className={`connector ${connected ? 'is-connected' : ''}`} data-testid={`connector-${c.id}`}>
      <button type="button" className="connector-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="connector-name">{c.name}</span>
        <Badge tone={connected ? 'ok' : c.status === 'error' ? 'bad' : 'neutral'}>
          {connected ? (c.simulated ? 'simulated' : 'connected') : c.status.replace('_', ' ')}
        </Badge>
        <span className="muted small connector-sum">{c.summary}</span>
      </button>
      {open && (
        <div className="connector-body">
          <div className="chips">
            {c.reads.map((r) => (
              <span key={r} className="chip">
                reads: {r}
              </span>
            ))}
            {c.writes.map((w) => (
              <span key={w} className="chip chip-write">
                can propose: {w}
              </span>
            ))}
          </div>
          {c.account?.label && <p className="small">Account: {c.account.label}</p>}
          {c.auth === 'token' ? (
            <TokenForm c={c} onDone={onChanged} />
          ) : (
            <div className="stack">
              <p className="small">
                Redirect URI: <code>{c.oauth?.redirect_uri}</code>
              </p>
              <ReasonButton
                className="btn btn-primary"
                reason={c.oauth?.configured ? null : 'Set the client ID on the server first (steps below)'}
                onClick={async () => {
                  setErr(null);
                  try {
                    window.location.assign((await api.spotifyStart()).auth_url);
                  } catch (x) {
                    setErr(x);
                  }
                }}
              >
                {connected ? 'Reconnect' : 'Connect'} {c.name}
              </ReasonButton>
            </div>
          )}
          <details className="small">
            <summary>How to set this up</summary>
            <ol>
              {c.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </details>
          {connected && c.resource && <ResourcePicker c={c} />}
          {connected && c.account && (
            <button
              type="button"
              className="btn btn-small btn-danger-ghost"
              onClick={async () => {
                if (!window.confirm(`Disconnect ${c.name}? The stored credentials are erased.`)) return;
                try {
                  await api.disconnect(c.account!.account_id);
                  onChanged();
                } catch (x) {
                  setErr(x);
                }
              }}
            >
              Disconnect
            </button>
          )}
          <ErrorBox error={err} />
        </div>
      )}
    </li>
  );
}

export function AppConnectors() {
  const list = useAsync(() => api.connectors(), []);
  return (
    <Card title="Apps" id="connect-apps">
      <p className="muted small">
        More tools for JARVIS. Each app only gets what you grant here, and every change it proposes follows your approval
        rules.
      </p>
      <Async state={list}>
        {(items) => {
          const fams = [...new Set(items.map((c) => c.family))];
          return (
            <div className="stack">
              {fams.map((f) => (
                <section key={f}>
                  <h4 className="family-head">{FAMILY[f] ?? f}</h4>
                  <ul className="plain connectors">
                    {items
                      .filter((c) => c.family === f)
                      .map((c) => (
                        <ConnectorCard key={c.id} c={c} onChanged={() => void list.reload()} />
                      ))}
                  </ul>
                </section>
              ))}
            </div>
          );
        }}
      </Async>
    </Card>
  );
}
