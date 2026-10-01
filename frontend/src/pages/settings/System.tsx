import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Backup, Budget, RestorePreview, Schedule } from '../../api/types';
import { Async, Badge, Card, ErrorBox, KV, ReasonButton, StateBadge, Toggle } from '../../components/ui';
import { fmtBytes, fmtDateTime, fmtRelative, fmtUsd } from '../../lib/format';
import { useAsync, useInterval } from '../../lib/hooks';
import { currentSubscription, pushUnsupportedReason, subscribePush, unsubscribePush } from '../../lib/push';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function weekdaysLabel(w: Schedule['weekdays']): string {
  if (w === null || w === undefined) return 'Every day';
  if (typeof w === 'string') return w;
  if (w.length === 7 || w.length === 0) return 'Every day';
  return w.map((d) => DAYS[d] ?? String(d)).join(', ');
}
function clock(h: number, m: number): string {
  const d = new Date(Date.UTC(2000, 0, 1, h, m));
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });
}

export function SchedulesSection() {
  const schedules = useAsync(() => api.schedules(), []);
  const worker = useAsync(() => api.workerStatus(), []);
  useInterval(() => void worker.reload(), 30000);
  const [err, setErr] = useState<unknown>(null);
  const w = worker.data;
  return (
    <div className="stack">
      <Card title="Scheduler worker">
        <div className="notice notice-warn">
          Scheduled briefings run on the JARVIS host. <strong>If this laptop sleeps or is offline, scheduled runs do not happen</strong>; missed runs are
          coalesced into one labeled catch-up when it wakes (if still useful). For reliable schedules, run JARVIS on an always-on machine you control.
        </div>
        <Async state={worker}>
          {(ws) => (
            <KV
              items={[
                ['Status', ws.alive ? <Badge tone="ok">alive</Badge> : <Badge tone="bad">not running</Badge>],
                ['Last heartbeat', ws.last_beat_at ? `${fmtRelative(ws.last_beat_at)} (${fmtDateTime(ws.last_beat_at)})` : 'never'],
                ['Worker', ws.worker_id ? `${ws.worker_id}${ws.pid ? ` · pid ${ws.pid}` : ''}` : '—'],
              ]}
            />
          )}
        </Async>
        {w && !w.alive && <p className="bad-text small">No worker heartbeat: schedules will not fire until the worker process is started.</p>}
      </Card>
      <Card title="Schedules">
        <p className="hint">Defaults (confirm before enabling): 6:30 AM Daily Command · 8:30 PM Nightly Debrief · Sunday 7:00 PM Weekly War Room — America/Chicago.</p>
        <ErrorBox error={err} />
        <Async state={schedules} empty="No schedules defined on the server.">
          {(list) => (
            <ul className="stack plain">
              {list.map((s) => (
                <li key={s.id} className="card schedule" data-testid={`schedule-${s.key}`}>
                  <div className="row gap wrap">
                    <strong className="grow">{s.label}</strong>
                    <span>
                      {clock(s.hour, s.minute)} · {weekdaysLabel(s.weekdays)} · {s.tz}
                    </span>
                  </div>
                  <Toggle
                    label={s.enabled ? 'Enabled' : 'Disabled'}
                    checked={s.enabled}
                    onChange={async (v) => {
                      setErr(null);
                      try {
                        const ns = await api.patchSchedule(s.id, { enabled: v });
                        schedules.setData((l) => l?.map((x) => (x.id === s.id ? ns : x)));
                      } catch (e) {
                        setErr(e);
                      }
                    }}
                  />
                  <KV
                    items={[
                      ['Next run', s.enabled && s.next_run_at ? fmtDateTime(s.next_run_at, s.tz) : s.enabled ? '—' : 'disabled'],
                      ['Last run', s.last_run_at ? `${fmtDateTime(s.last_run_at, s.tz)} (${s.last_status ?? '?'})` : 'never'],
                      ['Last success', s.last_success_at ? fmtDateTime(s.last_success_at, s.tz) : 'never'],
                      ['Destination', s.destination ?? 'in-app'],
                    ]}
                  />
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Card>
    </div>
  );
}

export function NotificationSettingsSection() {
  const config = useAsync(() => api.pushConfig(), []);
  const [perm, setPerm] = useState<NotificationPermission | 'unsupported'>(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);
  const [sub, setSub] = useState<PushSubscription | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const reason = pushUnsupportedReason();
  useEffect(() => {
    void currentSubscription().then(setSub).catch(() => undefined);
  }, []);
  return (
    <Card title="Notifications" id="notifications">
      <h4>In-app inbox</h4>
      <p className="hint">Always on. The bell in the header shows unread count.</p>
      <h4>Browser notifications (while JARVIS is open)</h4>
      {perm === 'unsupported' ? (
        <p className="muted">Not supported in this browser.</p>
      ) : (
        <div className="row gap wrap">
          <span>
            Permission: <StateBadge state={perm === 'granted' ? 'ok' : perm === 'denied' ? 'failed' : 'pending'} /> {perm}
          </span>
          <ReasonButton
            className="btn"
            reason={perm === 'granted' ? 'Already allowed' : perm === 'denied' ? 'Blocked — change it in browser site settings' : null}
            onClick={async () => setPerm(await Notification.requestPermission())}
          >
            Allow browser notifications
          </ReasonButton>
        </div>
      )}
      <h4>Web Push (when JARVIS is closed)</h4>
      <Async state={config}>
        {(c) => {
          const serverReason = !c.supported ? c.reason ?? 'Push is not configured on the server.' : !c.vapid_public_key ? 'Server has no VAPID key configured.' : null;
          const why = reason ?? serverReason;
          return (
            <>
              {why && <p className="warn-text">{why}</p>}
              <div className="row gap wrap">
                {sub ? (
                  <>
                    <Badge tone="ok">subscribed on this device</Badge>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        setErr(null);
                        try {
                          await unsubscribePush();
                          setSub(null);
                        } catch (e) {
                          setErr(e);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Unsubscribe
                    </button>
                  </>
                ) : (
                  <ReasonButton
                    className="btn btn-primary"
                    reason={why ? 'Unavailable (see above)' : busy ? 'Subscribing…' : null}
                    onClick={async () => {
                      setBusy(true);
                      setErr(null);
                      try {
                        setSub(await subscribePush(c.vapid_public_key!));
                        setPerm(Notification.permission);
                      } catch (e) {
                        setErr(new Error(errorMessage(e)));
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    Enable push on this device
                  </ReasonButton>
                )}
              </div>
              <p className="hint">
                Lock-screen content is kept minimal. Status shown per notification: generated → queued → accepted by push service → opened. “Accepted” does not
                prove you saw it.
              </p>
            </>
          );
        }}
      </Async>
      <ErrorBox error={err} />
    </Card>
  );
}

function num(v: string): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function renderUnknown(v: unknown): React.ReactNode {
  if (v === null || v === undefined) return <span className="muted">not reported</span>;
  if (typeof v !== 'object') return String(v);
  const entries = Object.entries(v as Record<string, unknown>);
  if (!entries.length) return <span className="muted">none yet</span>;
  return (
    <ul className="plain small">
      {entries.map(([k, x]) => (
        <li key={k}>
          <span className="muted">{k}:</span> {typeof x === 'object' ? JSON.stringify(x) : String(x)}
        </li>
      ))}
    </ul>
  );
}

function BrainCard() {
  const brain = useAsync(() => api.brain(), []);
  return (
    <Card title="JARVIS brain">
      <Async state={brain}>
        {(b) => (
          <>
            <p>
              <strong className={b.mode === 'v2_agent' ? 'ok-text' : 'warn-text'}>
                {b.mode === 'v2_agent' ? 'V2 reasoning online' : 'Offline mode (rule-based)'}
              </strong>{' '}
              <span className="muted small">· model {b.model}</span>
            </p>
            {b.reason && <p className="warn-text small">Why offline: {b.reason}</p>}
            <p className="muted small">
              In V2, JARVIS reads each command, decides what you want and picks its tools ({b.tools?.length ?? 0} built in, plus web
              search). {b.notes}
            </p>
            <label className="auto-send">
              <input
                type="checkbox"
                checked={b.web_research}
                onChange={async (e) => {
                  await api.setResearch({ web_enabled: e.target.checked });
                  brain.setData({ ...b, web_research: e.target.checked });
                }}
              />{' '}
              Allow web research (search + read pages)
            </label>
            <label className="field small">
              <span>
                Web searches per command: <strong>{b.max_searches}</strong> <span className="muted">(~$0.01 each)</span>
              </span>
              <input
                type="range"
                min={3}
                max={50}
                step={1}
                value={b.max_searches ?? 15}
                onChange={(e) => brain.setData({ ...b, max_searches: Number(e.target.value) })}
                onPointerUp={(e) => void api.setResearch({ max_searches: Number(e.currentTarget.value) })}
                onKeyUp={(e) => void api.setResearch({ max_searches: Number(e.currentTarget.value) })}
              />
            </label>
          </>
        )}
      </Async>
    </Card>
  );
}

export function BudgetSection() {
  const budget = useAsync(() => api.budget(), []);
  const [draft, setDraft] = useState<Budget | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (budget.data) setDraft(budget.data);
  }, [budget.data]);
  return (
    <>
    <BrainCard />
    <Card title="Budget & model usage">
      <Async state={budget}>
        {(b) => (
          <>
            <KV
              items={[
                ['Spent today', `${fmtUsd(b.spent_today_usd)} of ${fmtUsd(b.daily_limit_usd)}`],
                ['Spent this month', `${fmtUsd(b.spent_month_usd)} of ${fmtUsd(b.monthly_limit_usd)}`],
                ['Reserved (in-flight)', fmtUsd(b.reserved_usd)],
                ['Estimated vs measured', renderUnknown(b.measured_vs_estimated)],
              ]}
            />
            <div className="meter" role="meter" aria-label="Daily budget used" aria-valuemin={0} aria-valuemax={b.daily_limit_usd || 1} aria-valuenow={b.spent_today_usd}>
              <span style={{ width: `${Math.min(100, b.daily_limit_usd ? (b.spent_today_usd / b.daily_limit_usd) * 100 : 0)}%` }} />
            </div>
            {draft && (
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  setErr(null);
                  setSaved(false);
                  try {
                    const { paid_inference_enabled, background_paid_enabled, daily_limit_usd, monthly_limit_usd, max_output_tokens, max_tool_calls, max_specialists, effort } = draft;
                    budget.setData(await api.patchBudget({ paid_inference_enabled, background_paid_enabled, daily_limit_usd, monthly_limit_usd, max_output_tokens, max_tool_calls, max_specialists, effort }));
                    setSaved(true);
                  } catch (x) {
                    setErr(x);
                  }
                }}
              >
                <Toggle label="Paid inference enabled" checked={draft.paid_inference_enabled} onChange={(v) => setDraft({ ...draft, paid_inference_enabled: v })} description="When off, JARVIS will not call paid models." />
                <Toggle label="Allow paid inference in background jobs" checked={draft.background_paid_enabled} onChange={(v) => setDraft({ ...draft, background_paid_enabled: v })} />
                <div className="grid-form">
                  <label className="field">
                    <span>Daily limit (USD)</span>
                    <input type="number" min={0} step={0.5} value={draft.daily_limit_usd} onChange={(e) => setDraft({ ...draft, daily_limit_usd: num(e.target.value) })} />
                  </label>
                  <label className="field">
                    <span>Monthly limit (USD)</span>
                    <input type="number" min={0} step={1} value={draft.monthly_limit_usd} onChange={(e) => setDraft({ ...draft, monthly_limit_usd: num(e.target.value) })} />
                  </label>
                  <label className="field">
                    <span>Max output tokens</span>
                    <input type="number" min={0} value={draft.max_output_tokens} onChange={(e) => setDraft({ ...draft, max_output_tokens: num(e.target.value) })} />
                  </label>
                  <label className="field">
                    <span>Max tool calls / run</span>
                    <input type="number" min={0} value={draft.max_tool_calls} onChange={(e) => setDraft({ ...draft, max_tool_calls: num(e.target.value) })} />
                  </label>
                  <label className="field">
                    <span>Max specialists / run</span>
                    <input type="number" min={0} value={draft.max_specialists} onChange={(e) => setDraft({ ...draft, max_specialists: num(e.target.value) })} />
                  </label>
                  <label className="field">
                    <span>Effort</span>
                    <select value={draft.effort} onChange={(e) => setDraft({ ...draft, effort: e.target.value })}>
                      {[...new Set(['low', 'medium', 'high', draft.effort])].map((x) => (
                        <option key={x}>{x}</option>
                      ))}
                    </select>
                  </label>
                </div>
                <button className="btn btn-primary" type="submit">
                  Save budget
                </button>
                {saved && <span className="ok-text"> Saved.</span>}
                <ErrorBox error={err} />
              </form>
            )}
            <details>
              <summary>Pricing table used for estimates</summary>
              {renderUnknown(b.pricing)}
            </details>
          </>
        )}
      </Async>
    </Card>
    </>
  );
}

function RestoreFlow({ b }: { b: Backup }) {
  const [preview, setPreview] = useState<RestorePreview | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div>
      {!preview ? (
        <button
          type="button"
          className="btn btn-small"
          onClick={async () => {
            setErr(null);
            try {
              setPreview(await api.restorePreview(b.id));
            } catch (e) {
              setErr(e);
            }
          }}
        >
          Preview restore
        </button>
      ) : (
        <div className="notice notice-warn">
          <strong>{preview.compatible ? 'Restore preview' : 'Incompatible backup — cannot restore'}</strong>
          <KV items={Object.entries(preview.counts).map(([k, v]) => [k, String(v)])} />
          {preview.will_disable.length > 0 && <p>Will be disabled after restore (no automatic re-runs): {preview.will_disable.join(', ')}</p>}
          {preview.pre_restore_backup && <p>A safety backup of the current data is taken first.</p>}
          {preview.compatible && (
            <>
              <label className="inline small">
                <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I understand this replaces current data.
              </label>
              <ReasonButton
                className="btn btn-danger"
                reason={!confirm ? 'Tick the confirmation first' : busy ? 'Restoring…' : null}
                onClick={async () => {
                  setBusy(true);
                  setErr(null);
                  try {
                    const r = await api.restore(b.id);
                    setDone(`Restored. Pre-restore backup: ${r.pre_restore_backup_id}. Reload the app.`);
                  } catch (e) {
                    setErr(e);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Restore this backup
              </ReasonButton>
            </>
          )}
          <button type="button" className="btn btn-small" onClick={() => setPreview(null)}>
            Close
          </button>
        </div>
      )}
      {done && <p className="ok-text">{done}</p>}
      <ErrorBox error={err} />
    </div>
  );
}

export function BackupsSection() {
  const backups = useAsync(() => api.backups(), []);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Card
      title="Backups"
      actions={
        <button
          type="button"
          className="btn btn-primary btn-small"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setErr(null);
            try {
              await api.createBackup();
              void backups.reload();
            } catch (e) {
              setErr(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? 'Creating…' : 'Create backup'}
        </button>
      }
    >
      <p className="hint">Backups exclude API keys and OAuth tokens by default. Restores never revive executed jobs, old approvals or disabled automations.</p>
      <ErrorBox error={err} />
      <Async state={backups} empty="No backups yet.">
        {(list) => (
          <ul className="stack plain">
            {list.map((b) => (
              <li key={b.id} className="card">
                <div className="row gap wrap">
                  <strong className="grow">{b.created_at ? fmtDateTime(b.created_at) : b.id}</strong>
                  <span className="muted small">
                    {fmtBytes(b.bytes)} · schema {String(b.schema_version)}
                  </span>
                </div>
                <p className="muted small mono">sha256 {b.sha256}</p>
                <p className="muted small mono">{b.path}</p>
                <RestoreFlow b={b} />
              </li>
            ))}
          </ul>
        )}
      </Async>
    </Card>
  );
}

export function DoctorSection() {
  const doctor = useAsync(() => api.doctor(), []);
  const latency = useAsync(() => api.latency(), []);
  return (
    <div className="stack">
      <Card
        title="Diagnostics"
        actions={
          <button type="button" className="btn btn-small" onClick={() => void doctor.reload()}>
            Re-run
          </button>
        }
      >
        <Async state={doctor} isEmpty={(d) => d.checks.length === 0} empty="No checks reported.">
          {(d) => (
            <ul className="list">
              {d.checks.map((c) => (
                <li key={c.name} className="list-row doctor-row">
                  <StateBadge state={c.status} />
                  <div className="grow">
                    <strong>{c.name}</strong>
                    {c.detail && <p className="small">{c.detail}</p>}
                    {c.remedy && c.status !== 'ok' && <p className="small warn-text">Fix: {c.remedy}</p>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Card>
      <Card title="Measured latency">
        <Async state={latency} isEmpty={(d) => Object.keys(d).length === 0} empty="No latency measurements yet.">
          {(m) => (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Count</th>
                  <th scope="col">p50</th>
                  <th scope="col">p95</th>
                  <th scope="col">Last</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(m).map(([k, v]) => (
                  <tr key={k}>
                    <th scope="row">{k}</th>
                    <td>{v.count}</td>
                    <td>{v.p50_ms ?? '—'} ms</td>
                    <td>{v.p95_ms ?? '—'} ms</td>
                    <td>{v.last_ms ?? '—'} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </Card>
    </div>
  );
}

