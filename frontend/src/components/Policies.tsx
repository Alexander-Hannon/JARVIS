import { useState } from 'react';
import { api } from '../api/client';
import type { ActionCatalogItem } from '../api/types';
import { useAsync } from '../lib/hooks';
import { Async, Badge, Card, ErrorBox } from './ui';

/* Rules let low-risk changes skip the approval queue. Anything that reaches other people, deletes, money, locks or
   internet access for the sandbox can never be automated; those action types simply aren't offered here. */
const ORIGINS: { key: string; label: string }[] = [
  { key: 'command', label: 'my own requests' },
  { key: 'mission', label: 'background missions' },
  { key: 'channel', label: 'Telegram messages' },
];

function NewRule({ catalog, onDone }: { catalog: ActionCatalogItem[]; onDone: () => void }) {
  const auto = catalog.filter((c) => c.automatable);
  const [type, setType] = useState(auto[0]?.action_type ?? '');
  const [label, setLabel] = useState('');
  const [origins, setOrigins] = useState<string[]>(['command']);
  const [cap, setCap] = useState(10);
  const [err, setErr] = useState<unknown>(null);
  return (
    <form
      className="rule-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setErr(null);
        try {
          await api.createPolicy({ action_type: type, label: label.trim() || auto.find((a) => a.action_type === type)?.label || type, origins, daily_cap: cap });
          setLabel('');
          onDone();
        } catch (x) {
          setErr(x);
        }
      }}
    >
      <h3>New rule</h3>
      <label className="field">
        <span>Approve automatically</span>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          {auto.map((a) => (
            <option key={a.action_type} value={a.action_type}>
              {a.label}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Name (optional)</span>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Tasks from me" />
      </label>
      <fieldset className="chips">
        <legend className="small muted">When it comes from</legend>
        {ORIGINS.map((o) => (
          <label key={o.key} className="chip">
            <input
              type="checkbox"
              checked={origins.includes(o.key)}
              onChange={(e) => setOrigins(e.target.checked ? [...origins, o.key] : origins.filter((x) => x !== o.key))}
            />{' '}
            {o.label}
          </label>
        ))}
      </fieldset>
      <label className="field">
        <span>At most per 24 hours</span>
        <input type="number" min={1} max={100} value={cap} onChange={(e) => setCap(Number(e.target.value))} />
      </label>
      <button type="submit" className="btn btn-primary" disabled={!origins.length}>
        Create rule
      </button>
      <ErrorBox error={err} />
    </form>
  );
}

export function Policies() {
  const data = useAsync(() => api.policies(), []);
  const [err, setErr] = useState<unknown>(null);
  return (
    <Card>
      <Async state={data}>
        {(d) => (
          <div className="stack">
            <p className="muted small">
              Approved-by-rule changes run right away and can be undone for {d.undo_window_minutes} minutes from Actions → History.
            </p>
            {d.rules.filter((r) => r.active).length === 0 && <p className="muted">No rules yet: every change asks you first.</p>}
            <ul className="plain rules">
              {d.rules.map((r) => (
                <li key={r.id} className={`rule ${r.active ? '' : 'inactive'}`}>
                  <div>
                    <strong>{r.label}</strong> <span className="muted small">{r.action_label}</span>{' '}
                    {!r.active && <Badge tone="neutral">off</Badge>}
                  </div>
                  <div className="small muted">
                    From {r.origins.map((o) => ORIGINS.find((x) => x.key === o)?.label ?? o).join(', ')}. Used {r.used_24h} of {r.daily_cap} in the last 24 hours.
                  </div>
                  {r.active && (
                    <button
                      type="button"
                      className="btn btn-small btn-danger-ghost"
                      onClick={async () => {
                        try {
                          await api.revokePolicy(r.id);
                          void data.reload();
                        } catch (x) {
                          setErr(x);
                        }
                      }}
                    >
                      Turn off
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <NewRule catalog={d.catalog} onDone={() => void data.reload()} />
            <details className="small">
              <summary>What always asks you</summary>
              <ul>
                {d.catalog
                  .filter((c) => !c.automatable)
                  .map((c) => (
                    <li key={c.action_type}>{c.label}</li>
                  ))}
              </ul>
            </details>
          </div>
        )}
      </Async>
      <ErrorBox error={err} />
    </Card>
  );
}
