import { useCallback, useState } from 'react';
import { api } from '../api/client';
import type { ActionHistoryEntry } from '../api/types';
import { ProposalCard } from '../components/ProposalCard';
import { PageHead } from '../components/PageHead';
import { Policies } from '../components/Policies';
import { Async, Badge, Card, ErrorBox, StateBadge, Tabs } from '../components/ui';
import { fmtDateTime, humanKey, humanValue } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { useConversation } from '../state/app';

function UndoButton({ h, onDone }: { h: ActionHistoryEntry; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const open = h.undo_until && Date.parse(h.undo_until) > Date.now() && !h.undone_by && h.state === 'verified';
  if (h.undone_by) return <Badge tone="neutral">undone</Badge>;
  if (!open || !h.action_id) return null;
  return (
    <>
      <button
        type="button"
        className="btn btn-small btn-primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.undoAction(h.action_id!);
            onDone();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        Undo (until {new Date(h.undo_until!).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })})
      </button>
      <ErrorBox error={err} />
    </>
  );
}

function HistoryEntry({ h, onChanged }: { h: ActionHistoryEntry; onChanged: () => void }) {
  const args = h.args ?? {};
  const tz = typeof args.timezone === 'string' ? args.timezone : typeof args.tz === 'string' ? args.tz : undefined;
  const r = h.receipt;
  return (
    <li className="history-entry card" data-testid="history-entry">
      <header className="card-head">
        <h3 className="card-title">{h.proposal_title}</h3>
        <div className="row gap">
          {h.auto_approved && <Badge tone="info" title="Approved by one of your automation rules">approved by your rule</Badge>}
          <UndoButton h={h} onDone={onChanged} />
          {h.state && <StateBadge state={h.state} />}
          {r?.simulated && (
            <Badge tone="warn" title="Receipt produced by a contract mock, not a live provider">
              SIMULATED / contract mock
            </Badge>
          )}
        </div>
      </header>
      <dl className="kv">
        <div className="kv-row">
          <dt>What was asked</dt>
          <dd>{(h.request_text as string | undefined) ?? <span className="muted">Not recorded</span>}</dd>
        </div>
        <div className="kv-row">
          <dt>Approved</dt>
          <dd>{h.approved_at ? fmtDateTime(h.approved_at) : <span className="muted">—</span>}</dd>
        </div>
        <div className="kv-row">
          <dt>Changed</dt>
          <dd>
            <code>{h.action_type ?? 'action'}</code> in {h.provider ?? 'provider'}
            {h.account_label ? ` · ${h.account_label}` : ''}
            {h.target?.name ? ` → ${h.target.name}` : ''}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Exact plan</dt>
          <dd>
            {Object.keys(args).length ? (
              <ul className="plain">
                {Object.entries(args).map(([k, v]) => (
                  <li key={k}>
                    <span className="muted">{humanKey(k)}:</span> {humanValue(k, v, tz)}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">No arguments recorded</span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Verified</dt>
          <dd>
            {r ? (
              <>
                {r.ok ? 'Yes' : 'No'}
                {r.method ? ` via ${r.method}` : ''}
                {r.verified_at ? ` at ${fmtDateTime(r.verified_at)}` : ''}
              </>
            ) : (
              <span className="muted">No receipt</span>
            )}
            {h.external_url && (
              <>
                {' · '}
                <a href={h.external_url} target="_blank" rel="noopener noreferrer">
                  Open ↗
                </a>
              </>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Undo</dt>
          <dd>
            {typeof h.undo === 'string' && h.undo ? h.undo : h.undoable ? 'Can be undone with a new approved compensating action.' : <span className="muted">No undo information reported</span>}
          </dd>
        </div>
        {h.attempts?.length > 0 && (
          <div className="kv-row">
            <dt>Attempts</dt>
            <dd>
              <ol className="plain">
                {h.attempts.map((a, i) => (
                  <li key={i}>
                    {a.at ? fmtDateTime(a.at) : `#${i + 1}`} — {a.state ?? '?'}
                    {a.error ? <span className="bad-text"> ({a.error})</span> : null}
                  </li>
                ))}
              </ol>
            </dd>
          </div>
        )}
      </dl>
    </li>
  );
}

export function ActionsPage() {
  const [tab, setTab] = useState<'active' | 'history' | 'policies'>('active');
  const proposals = useAsync(() => api.proposals('active'), []);
  const history = useAsync(() => (tab === 'history' ? api.actionHistory(100) : Promise.resolve([] as ActionHistoryEntry[])), [tab]);
  const conv = useConversation();
  useInterval(() => {
    if (tab === 'active') void proposals.reload();
  }, 20000);

  const correction = useCallback(
    async (text: string) => {
      await conv.send({ text, channel: 'typed', captured_at: new Date().toISOString(), speech_playing: false });
    },
    [conv],
  );

  return (
    <div className="page">
      <PageHead title="Actions" guide="approvals">
        Everything JARVIS wants to change waits here for you, unless one of your rules approves it.{' '}
        <button type="button" className="btn btn-small" onClick={() => void (tab === 'active' ? proposals.reload() : history.reload())}>
          Refresh
        </button>
      </PageHead>
      <Tabs
        label="Action center views"
        value={tab}
        onChange={setTab}
        tabs={[
          { key: 'active', label: `Awaiting approval${proposals.data ? ` (${proposals.data.length})` : ''}` },
          { key: 'history', label: 'History' },
          { key: 'policies', label: 'Rules' },
        ]}
      />
      {tab === 'policies' ? (
        <Policies />
      ) : tab === 'active' ? (
        <Async state={proposals} empty="Nothing awaiting approval. Proposals from commands will appear here.">
          {(list) => (
            <div className="stack">
              {list.map((p) => (
                <ProposalCard key={p.id} proposal={p} onCorrection={correction} />
              ))}
            </div>
          )}
        </Async>
      ) : (
        <Card>
          <Async state={history} empty="No executed actions yet.">
            {(list) => (
              <ul className="stack plain">
                {list.map((h, i) => (
                  <HistoryEntry key={h.id ?? `${h.proposal_id}-${i}`} h={h} onChanged={() => void history.reload()} />
                ))}
              </ul>
            )}
          </Async>
        </Card>
      )}
    </div>
  );
}
