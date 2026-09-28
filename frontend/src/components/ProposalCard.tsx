import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError, errorMessage } from '../api/client';
import type { Args, Proposal, ProposedItem } from '../api/types';
import { fmtDateTime, fmtRelative, humanKey, humanValue } from '../lib/format';
import { useIsPaused } from '../state/app';
import { Badge, ReasonButton, StateBadge } from './ui';

const TERMINAL: ReadonlySet<string> = new Set(['verified', 'partial', 'failed', 'unknown', 'rejected', 'expired', 'superseded', 'cancelled']);
const IN_PROGRESS: ReadonlySet<string> = new Set(['approved', 'queued', 'executing']);
const POLL_MS = 1500;
const POLL_MAX_MS = 5 * 60 * 1000;

function argTz(args: Args): string | undefined {
  const tz = args.timezone ?? args.tz;
  return typeof tz === 'string' ? tz : undefined;
}

const PREFERRED_ORDER = ['title', 'content', 'summary', 'date', 'due_date', 'due_datetime', 'due_string', 'start', 'end', 'time', 'timezone', 'tz', 'duration_minutes', 'duration', 'project', 'project_id', 'calendar', 'calendar_id', 'priority', 'description'];
function orderedKeys(args: Args, missing: string[]): string[] {
  const keys = new Set([...Object.keys(args), ...missing]);
  return [...keys].sort((a, b) => {
    const ia = PREFERRED_ORDER.indexOf(a);
    const ib = PREFERRED_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
}

function ArgsList({ item, editable }: { item: ProposedItem; editable: boolean }) {
  const tz = argTz(item.args);
  // Setup blockers (e.g. "Todoist not connected …") are shown in the Needs line, not as editable fields.
  const keys = orderedKeys(item.args, item.missing_fields.filter((m) => /^[a-z_]+$/.test(m)));
  return (
    <dl className="args">
      {keys.map((k) => {
        const missing = item.missing_fields.includes(k);
        return (
          <div key={k} className={`arg ${missing ? 'arg-missing' : ''}`}>
            <dt>{humanKey(k)}</dt>
            <dd>{missing && (item.args[k] === undefined || item.args[k] === null || item.args[k] === '') ? <strong>{editable ? 'Missing — edit to fill in' : 'Missing'}</strong> : humanValue(k, item.args[k], tz)}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function BeforeAfter({ item }: { item: ProposedItem }) {
  if (!item.before && !item.after) return null;
  const before = item.before ?? {};
  const after = item.after ?? {};
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  if (!keys.length) return null;
  return (
    <table className="before-after">
      <caption>Change</caption>
      <thead>
        <tr>
          <th scope="col">Field</th>
          <th scope="col">Before</th>
          <th scope="col">After</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => {
          const changed = JSON.stringify(before[k]) !== JSON.stringify(after[k]);
          return (
            <tr key={k} className={changed ? 'changed' : ''}>
              <th scope="row">{humanKey(k)}</th>
              <td>{humanValue(k, before[k])}</td>
              <td>{humanValue(k, after[k])}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function EditItemForm({ item, onSave, onCancel, busy }: { item: ProposedItem; onSave: (args: Args) => void; onCancel: () => void; busy: boolean }) {
  // Setup blockers (e.g. "Todoist not connected …") are shown in the Needs line, not as editable fields.
  const keys = orderedKeys(item.args, item.missing_fields.filter((m) => /^[a-z_]+$/.test(m)));
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(keys.map((k) => {
      const v = item.args[k];
      return [k, v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)];
    })),
  );
  const build = (): Args => {
    const out: Args = {};
    for (const k of keys) {
      const orig = item.args[k];
      const s = draft[k] ?? '';
      if (s === '' && (orig === undefined || orig === null)) continue;
      if (typeof orig === 'number') out[k] = s === '' ? null : Number(s);
      else if (typeof orig === 'boolean') out[k] = s === 'true';
      else if (orig !== null && typeof orig === 'object') {
        try {
          out[k] = JSON.parse(s);
        } catch {
          out[k] = s;
        }
      } else out[k] = s === '' ? null : s;
    }
    return out;
  };
  return (
    <form
      className="edit-item"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(build());
      }}
    >
      <p className="hint">Saving creates a new proposal version. Any earlier approval is void; you will approve the new version.</p>
      {keys.map((k) => (
        <label key={k} className={`field ${item.missing_fields.includes(k) ? 'field-missing' : ''}`}>
          <span>
            {humanKey(k)} <code className="muted">{k}</code>
            {item.missing_fields.includes(k) && <Badge tone="warn">required</Badge>}
          </span>
          {typeof item.args[k] === 'boolean' ? (
            <select value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          ) : (
            <input
              value={draft[k] ?? ''}
              type={typeof item.args[k] === 'number' ? 'number' : 'text'}
              onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
            />
          )}
        </label>
      ))}
      <div className="row gap">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save as new version'}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel edit
        </button>
      </div>
    </form>
  );
}

function Receipt({ item, onRetry, retrying, paused }: { item: ProposedItem; onRetry: () => void; retrying: boolean; paused: boolean }) {
  const r = item.receipt;
  const retryReason = paused ? 'Actions are paused' : retrying ? 'Retrying…' : null;
  switch (item.state) {
    case 'verified':
      return (
        <div className="receipt receipt-ok" data-testid={`receipt-${item.index}`}>
          <strong>Verified</strong>
          {r?.simulated && <Badge tone="warn" title="This receipt came from a contract mock, not a live provider">SIMULATED / contract mock</Badge>}
          {r?.method && <span> via {r.method}</span>}
          {r?.verified_at && <span className="muted"> · {fmtDateTime(r.verified_at)}</span>}
          {item.external_url && (
            <a href={item.external_url} target="_blank" rel="noopener noreferrer" className="ext-link">
              Open in {item.provider} ↗
            </a>
          )}
        </div>
      );
    case 'failed':
      return (
        <div className="receipt receipt-bad" data-testid={`receipt-${item.index}`}>
          <strong>Failed</strong>
          <span>{item.last_error ?? 'No error detail returned.'}</span>
          <ReasonButton className="btn btn-small" reason={retryReason} onClick={onRetry}>
            Retry this item
          </ReasonButton>
        </div>
      );
    case 'unknown':
      return (
        <div className="receipt receipt-warn" data-testid={`receipt-${item.index}`}>
          <strong>Outcome unknown</strong>
          <span>This may have reached {item.provider}; JARVIS is reconciling before any retry so it will not create a duplicate.</span>
          {item.last_error && <span className="muted">{item.last_error}</span>}
          <ReasonButton className="btn btn-small" reason={retryReason} onClick={onRetry}>
            Reconcile &amp; retry
          </ReasonButton>
        </div>
      );
    case 'conflict':
      return (
        <div className="receipt receipt-warn" data-testid={`receipt-${item.index}`}>
          <strong>Conflict</strong>
          <span>{item.last_error ?? 'The target changed since this was proposed. Edit to create a new version.'}</span>
        </div>
      );
    case 'queued':
    case 'executing':
    case 'approved':
      return (
        <div className="receipt" data-testid={`receipt-${item.index}`}>
          <strong>{item.state === 'executing' ? 'Executing' : item.state === 'queued' ? 'Queued' : 'Approved'}</strong>
          <span className="muted">Waiting for provider verification…</span>
        </div>
      );
    case 'paused':
      return (
        <div className="receipt receipt-warn" data-testid={`receipt-${item.index}`}>
          <strong>Paused</strong> <span>Actions are paused; this item will not run until you resume and re-approve.</span>
        </div>
      );
    default:
      return null;
  }
}

export interface ProposalCardProps {
  proposal: Proposal;
  onChanged?: (p: Proposal) => void;
  /** Sends a free-text correction as a command (used for assumption edits). */
  onCorrection?: (text: string) => Promise<void>;
  compact?: boolean;
}

export function ProposalCard({ proposal: initial, onChanged, onCorrection }: ProposalCardProps) {
  const [p, setP] = useState<Proposal>(initial);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'info' | 'warn' | 'bad'; text: string } | null>(null);
  const [polling, setPolling] = useState(false);
  const [editingAssumption, setEditingAssumption] = useState<number | null>(null);
  const [assumptionDraft, setAssumptionDraft] = useState('');
  const paused = useIsPaused();
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  useEffect(() => {
    // New data from the parent (e.g. list refresh) — accept newer versions only.
    setP((cur) => (initial.id !== cur.id || initial.version > cur.version ? initial : cur));
  }, [initial]);

  const update = useCallback((np: Proposal) => {
    setP(np);
    onChangedRef.current?.(np);
  }, []);

  // Poll after approval until terminal.
  useEffect(() => {
    if (!polling) return;
    const started = Date.now();
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const np = await api.proposal(p.id);
        if (stopped) return;
        update(np);
        if (TERMINAL.has(np.status) && !np.items.some((i) => IN_PROGRESS.has(i.state))) {
          setPolling(false);
          return;
        }
      } catch (e) {
        setNotice({ tone: 'warn', text: `Status check failed (${errorMessage(e)}); still trying.` });
      }
      if (Date.now() - started > POLL_MAX_MS) {
        setPolling(false);
        setNotice({ tone: 'warn', text: 'Stopped checking after 5 minutes. Refresh the Action center for the latest state.' });
        return;
      }
      timer = window.setTimeout(tick, POLL_MS);
    };
    let timer = window.setTimeout(tick, POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [polling, p.id, update]);

  const isOpen = p.status === 'proposed';
  const expired = p.status === 'expired' || (!!p.expires_at && new Date(p.expires_at).getTime() < Date.now() && isOpen);
  const selectable = (it: ProposedItem) => isOpen && !expired && it.missing_fields.length === 0 && it.state === 'proposed';
  const readyItems = useMemo(() => p.items.filter(selectable), [p]); // eslint-disable-line react-hooks/exhaustive-deps

  const approveReason = !isOpen
    ? `This proposal is ${p.status}`
    : expired
      ? 'This proposal has expired — ask again for a fresh plan'
      : paused
        ? 'Actions are paused — resume from the header to approve'
        : selected.size === 0
          ? 'Select at least one ready item'
          : busy
            ? 'Working…'
            : null;

  const refetch = async () => {
    const np = await api.proposal(p.id);
    update(np);
    return np;
  };

  const approve = async () => {
    setBusy('approve');
    setNotice(null);
    try {
      const res = await api.approve(p.id, p.version, p.hash, [...selected].sort((a, b) => a - b));
      update(res.proposal);
      setSelected(new Set());
      setPolling(true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const np = await refetch();
          setNotice({ tone: 'warn', text: `This plan changed since you reviewed it (now version ${np.version}). Nothing was executed. Review the current version and approve again.` });
        } catch (e2) {
          setNotice({ tone: 'bad', text: `This plan is stale and the refresh failed: ${errorMessage(e2)}` });
        }
        setSelected(new Set());
      } else if (e instanceof ApiError && e.status === 423) {
        setNotice({ tone: 'warn', text: 'Actions are paused. Nothing was executed. Resume actions from the header, then approve again.' });
      } else {
        setNotice({ tone: 'bad', text: `Approval failed: ${errorMessage(e)}` });
      }
    } finally {
      setBusy(null);
    }
  };

  const reject = async () => {
    setBusy('reject');
    setNotice(null);
    try {
      update(await api.reject(p.id));
    } catch (e) {
      setNotice({ tone: 'bad', text: `Reject failed: ${errorMessage(e)}` });
    } finally {
      setBusy(null);
    }
  };

  const revise = async (index: number, args: Args | null) => {
    setBusy('revise');
    setNotice(null);
    try {
      const np = await api.revise(p.id, [args ? { index, args } : { index, remove: true }]);
      update(np);
      setEditing(null);
      setSelected(new Set());
      setNotice({ tone: 'info', text: `Saved as version ${np.version}. Previous approval (if any) is void — review and approve this version.` });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        await refetch().catch(() => undefined);
        setNotice({ tone: 'warn', text: 'This plan changed while you were editing. Showing the current version.' });
      } else setNotice({ tone: 'bad', text: `Edit failed: ${errorMessage(e)}` });
    } finally {
      setBusy(null);
    }
  };

  const retry = async (index: number) => {
    setBusy(`retry-${index}`);
    setNotice(null);
    try {
      update(await api.retry(p.id, [index]));
      setPolling(true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 423) setNotice({ tone: 'warn', text: 'Actions are paused. Resume to retry.' });
      else setNotice({ tone: 'bad', text: `Retry failed: ${errorMessage(e)}` });
    } finally {
      setBusy(null);
    }
  };

  const toggle = (i: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      return n;
    });

  return (
    <article className={`proposal proposal-${p.status}`} data-testid="proposal-card" aria-labelledby={`p-${p.id}-title`}>
      <header className="proposal-head">
        <div>
          <h3 id={`p-${p.id}-title`}>{p.title}</h3>
          <div className="meta">
            <StateBadge state={p.status} />
            <span data-testid="proposal-version">v{p.version}</span>
            {p.expires_at && <span className={expired ? 'bad-text' : ''}>{expired ? 'expired' : 'expires'} {fmtRelative(p.expires_at)}</span>}
            {polling && <span role="status">Checking execution status…</span>}
          </div>
        </div>
      </header>
      {p.summary && <p className="proposal-summary">{p.summary}</p>}

      {notice && (
        <div className={`notice notice-${notice.tone}`} role={notice.tone === 'info' ? 'status' : 'alert'} data-testid="proposal-notice">
          {notice.text}
        </div>
      )}

      {p.assumptions.length > 0 && (
        <div className="assumptions">
          <h4>Assumptions</h4>
          <ul>
            {p.assumptions.map((a, i) => (
              <li key={i}>
                {editingAssumption === i ? (
                  <form
                    className="inline"
                    onSubmit={async (e) => {
                      e.preventDefault();
                      if (!onCorrection) return;
                      setBusy('assumption');
                      try {
                        await onCorrection(`Correction to "${p.title}" (v${p.version}): the assumption "${a}" is wrong — ${assumptionDraft}`);
                        setEditingAssumption(null);
                        setNotice({ tone: 'info', text: 'Correction sent. JARVIS will produce a new version for approval.' });
                      } finally {
                        setBusy(null);
                      }
                    }}
                  >
                    <input aria-label="Corrected assumption" value={assumptionDraft} onChange={(e) => setAssumptionDraft(e.target.value)} />
                    <button className="btn btn-small btn-primary" type="submit" disabled={!assumptionDraft.trim() || !!busy}>
                      Send correction
                    </button>
                    <button className="btn btn-small" type="button" onClick={() => setEditingAssumption(null)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  <>
                    <span>{a}</span>{' '}
                    {onCorrection && isOpen && (
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => {
                          setEditingAssumption(i);
                          setAssumptionDraft(a);
                        }}
                      >
                        Correct
                      </button>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {p.questions.length > 0 && (
        <div className="questions">
          <h4>Questions</h4>
          <ul>
            {p.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      )}

      <ol className="items">
        {p.items.map((it) => {
          const canSelect = selectable(it);
          const why = !isOpen || expired ? null : it.missing_fields.length ? `Needs ${it.missing_fields.map(humanKey).join(', ')}` : it.state !== 'proposed' ? `Item is ${it.state}` : null;
          return (
            <li key={it.index} className={`item ${it.missing_fields.length ? 'item-missing' : ''}`} data-testid={`item-${it.index}`}>
              <div className="item-head">
                {isOpen && (
                  <input
                    type="checkbox"
                    className="item-check"
                    aria-label={`Select item ${it.index + 1}: ${it.action_type}`}
                    checked={selected.has(it.index)}
                    disabled={!canSelect}
                    onChange={() => toggle(it.index)}
                  />
                )}
                <div className="item-title">
                  <code className="action-type">{it.action_type}</code>
                  <span className="target">
                    {it.provider}
                    {it.account_label ? ` · ${it.account_label}` : ''}
                    {it.target?.name ? ` → ${it.target.name}` : ''}
                  </span>
                </div>
                <StateBadge state={it.state} />
              </div>
              {why && <p className="reason warn-text">{why}</p>}
              {editing === it.index ? (
                <EditItemForm item={it} busy={busy === 'revise'} onCancel={() => setEditing(null)} onSave={(args) => void revise(it.index, args)} />
              ) : (
                <>
                  <ArgsList item={it} editable={isOpen && !expired} />
                  <BeforeAfter item={it} />
                  {it.expected_effect && <p className="effect">Effect: {it.expected_effect}</p>}
                  {it.reason && <p className="muted small">Why: {it.reason}</p>}
                  {isOpen && !expired && (
                    <div className="row gap">
                      <button type="button" className="btn btn-small" onClick={() => setEditing(it.index)} disabled={!!busy}>
                        Edit
                      </button>
                      {p.items.length > 1 && (
                        <button type="button" className="btn btn-small btn-ghost" onClick={() => void revise(it.index, null)} disabled={!!busy}>
                          Remove from plan
                        </button>
                      )}
                    </div>
                  )}
                </>
              )}
              <Receipt item={it} paused={paused} retrying={busy === `retry-${it.index}`} onRetry={() => void retry(it.index)} />
            </li>
          );
        })}
      </ol>

      {isOpen && (
        <footer className="proposal-actions">
          {readyItems.length > 1 && (
            <button type="button" className="btn btn-ghost" onClick={() => setSelected(new Set(readyItems.map((i) => i.index)))}>
              Select all ready ({readyItems.length})
            </button>
          )}
          <ReasonButton className="btn btn-primary btn-large" reason={approveReason} onClick={() => void approve()} data-testid="approve-btn">
            {busy === 'approve' ? 'Approving…' : `Approve selected (${selected.size})`}
          </ReasonButton>
          <button type="button" className="btn btn-danger-ghost" onClick={() => void reject()} disabled={!!busy}>
            Reject
          </button>
        </footer>
      )}
    </article>
  );
}
