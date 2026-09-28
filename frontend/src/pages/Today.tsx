import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { Plan, Task, Today } from '../api/types';
import { Async, Card, Empty, ErrorBox, SourceBadge } from '../components/ui';
import { fmtDate, fmtRelative, fmtTime, humanValue, localTz, textOf } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { useConversation } from '../state/app';

function taskDue(t: Task): string {
  const d = t.due;
  if (!d) return '';
  if (typeof d === 'string') return humanValue('due', d);
  return humanValue('due', d.datetime ?? d.date ?? '');
}

function PlanEditor({ plan, onSaved }: { plan: Plan | null; onSaved: (p: Plan) => void }) {
  const [editing, setEditing] = useState(false);
  const [mission, setMission] = useState(plan?.mission ?? '');
  const [pri, setPri] = useState<string[]>(() => {
    const p = [...(plan?.priorities ?? [])];
    while (p.length < 3) p.push('');
    return p.slice(0, 3);
  });
  const [firstAction, setFirstAction] = useState(plan?.first_action ?? '');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  if (!editing) {
    return (
      <Card
        title="Mission"
        actions={
          <button type="button" className="btn btn-small" onClick={() => setEditing(true)}>
            {plan ? 'Edit plan' : 'Set plan'}
          </button>
        }
      >
        {plan ? (
          <>
            <p className="mission" data-testid="mission">{plan.mission || <span className="muted">No mission set</span>}</p>
            <h3 className="subhead">Top 3 priorities</h3>
            {plan.priorities.length ? (
              <ol className="priorities">
                {plan.priorities.slice(0, 3).map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ol>
            ) : (
              <Empty>No priorities set.</Empty>
            )}
            {plan.first_action && (
              <p>
                <span className="muted">First action:</span> {plan.first_action}
              </p>
            )}
            <p className="muted small">
              Plan v{plan.version} · by {plan.author}
            </p>
          </>
        ) : (
          <Empty>No plan for today yet. Set a mission and three priorities, or generate a Daily Command briefing.</Empty>
        )}
      </Card>
    );
  }

  return (
    <Card title="Edit today's plan">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            const saved = await api.putPlan({
              mission: mission.trim(),
              priorities: pri.map((p) => p.trim()).filter(Boolean),
              constraints: plan?.constraints ?? [],
              first_action: firstAction.trim() || null,
            });
            onSaved(saved);
            setEditing(false);
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          <span>Mission</span>
          <input value={mission} onChange={(e) => setMission(e.target.value)} required />
        </label>
        {pri.map((p, i) => (
          <label key={i} className="field">
            <span>Priority {i + 1}</span>
            <input value={p} onChange={(e) => setPri(pri.map((x, j) => (j === i ? e.target.value : x)))} />
          </label>
        ))}
        <label className="field">
          <span>First action</span>
          <input value={firstAction} onChange={(e) => setFirstAction(e.target.value)} />
        </label>
        <ErrorBox error={err} />
        <div className="row gap">
          <button type="submit" className="btn btn-primary" disabled={busy || !mission.trim()}>
            {busy ? 'Saving…' : 'Save new plan version'}
          </button>
          <button type="button" className="btn" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );
}

function QuickCapture() {
  const conv = useConversation();
  const [text, setText] = useState('');
  const [result, setResult] = useState<{ text: string; proposal: boolean } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  return (
    <Card title="Quick capture">
      <form
        className="inline"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!text.trim()) return;
          setErr(null);
          const captured_at = new Date().toISOString();
          const r = await conv.send({ text: text.trim(), channel: 'typed', captured_at, speech_playing: false });
          if (r) {
            setResult({ text: r.message.text, proposal: !!r.proposal });
            setText('');
          } else setErr('The command failed — see the Command console for details.');
          ref.current?.focus();
        }}
      >
        <input ref={ref} aria-label="Quick capture" placeholder="Add a task, note, or question…" value={text} onChange={(e) => setText(e.target.value)} />
        <button type="submit" className="btn btn-primary" disabled={conv.sending || !text.trim()}>
          {conv.sending ? 'Sending…' : 'Capture'}
        </button>
      </form>
      {result && (
        <div className="notice notice-info" role="status">
          {result.text}
          {result.proposal && (
            <>
              {' '}
              <Link to="/actions">Review the proposal in Actions →</Link>
            </>
          )}
        </div>
      )}
      {err && <p className="bad-text">{err}</p>}
    </Card>
  );
}

export function TodayPage() {
  const today = useAsync(() => api.today(), []);
  useInterval(() => void today.reload(), 60000);

  return (
    <div className="page">
      <Async state={today}>
        {(t: Today) => (
          <>
            <header className="page-head">
              <div>
                <h1>{fmtDate(t.date)}</h1>
                <p className="muted">
                  {t.tz}
                  {t.tz !== localTz() && <span className="warn-text"> · this device is in {localTz()}</span>} · updated {fmtRelative(t.now)}
                </p>
              </div>
            </header>
            <div className="sources" aria-label="Source status">
              {Object.keys(t.sources).length === 0 ? (
                <span className="warn-text">No source status reported.</span>
              ) : (
                Object.entries(t.sources).map(([k, v]) => <SourceBadge key={k} name={k} info={v} />)
              )}
            </div>
            <div className="grid-2">
              <PlanEditor plan={t.plan} onSaved={(p) => today.setData((d) => (d ? { ...d, plan: p } : d))} />
              <QuickCapture />
            </div>
            <div className="grid-2">
              <Card title={`Agenda (${t.agenda.length})`}>
                {t.agenda.length ? (
                  <ul className="list">
                    {t.agenda.map((ev, i) => (
                      <li key={ev.id ?? i} className="list-row">
                        <span className="time">{ev.all_day ? 'All day' : `${fmtTime(ev.start, t.tz)}–${fmtTime(ev.end, t.tz)}`}</span>
                        <span>{ev.title ?? ev.summary ?? '(untitled)'}</span>
                        {ev.calendar && <span className="muted small">{ev.calendar}</span>}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>{t.sources.calendar && t.sources.calendar.status !== 'ok' ? `No agenda shown — calendar is ${t.sources.calendar.status.replace('_', ' ')}.` : 'No events today.'}</Empty>
                )}
              </Card>
              <Card title={`Due / overdue (${t.tasks_due.length + (t.overdue?.length ?? 0)})`}>
                {t.tasks_due.length + (t.overdue?.length ?? 0) ? (
                  <ul className="list">
                    {[...(t.overdue ?? []).map((x) => ({ ...x, _overdue: true })), ...t.tasks_due].map((task, i) => (
                      <li key={task.id ?? i} className="list-row">
                        <span>{task.content ?? task.title ?? textOf(task)}</span>
                        <span className={task._overdue ? 'warn-text small' : 'muted small'}>{task._overdue ? 'overdue · ' : ''}{taskDue(task)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>{t.sources.todoist && t.sources.todoist.status !== 'ok' ? `No tasks shown — Todoist is ${t.sources.todoist.status.replace('_', ' ')}.` : 'Nothing due today.'}</Empty>
                )}
              </Card>
            </div>
            <div className="grid-3">
              <Card title={`Deadlines (${t.deadlines.length})`}>
                {t.deadlines.length ? (
                  <ul className="list">
                    {t.deadlines.map((d, i) => (
                      <li key={d.id ?? i} className="list-row">
                        <span>{d.title ?? textOf(d)}</span>
                        <span className="muted small">
                          {d.course ? `${d.course} · ` : ''}
                          {d.due_at ? humanValue('due_at', d.due_at, t.tz) : ''}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>{t.sources.deadlines && t.sources.deadlines.status !== 'ok' ? `No deadlines shown — feed is ${t.sources.deadlines.status.replace('_', ' ')}.` : 'No upcoming deadlines.'}</Empty>
                )}
              </Card>
              <Card title={`Conflicts (${t.conflicts.length})`}>
                {t.conflicts.length ? (
                  <ul className="list">
                    {t.conflicts.map((c, i) => (
                      <li key={i} className="list-row warn-text">
                        {c}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>No conflicts detected in connected sources.</Empty>
                )}
              </Card>
              <Card title={`Work windows (${t.work_windows.length})`}>
                {t.work_windows.length ? (
                  <ul className="list">
                    {t.work_windows.map((w, i) => (
                      <li key={i} className="list-row">
                        <span className="time">
                          {fmtTime(w.start, t.tz)}–{fmtTime(w.end, t.tz)}
                        </span>
                        <span>{w.label}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>No open work windows computed.</Empty>
                )}
              </Card>
            </div>
          </>
        )}
      </Async>
      {today.error && today.data === undefined ? <QuickCapture /> : null}
    </div>
  );
}
