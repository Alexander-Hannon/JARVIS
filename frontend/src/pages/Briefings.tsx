import { useState } from 'react';
import { api } from '../api/client';
import type { Briefing, BriefingKind, SourceInfo } from '../api/types';
import { Async, Badge, Card, Empty, ErrorBox, SourceBadge, Tabs } from '../components/ui';
import { fmtDate, fmtDateTime, textOf } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { useSpeech } from '../state/app';

const KIND_LABEL: Record<BriefingKind, string> = {
  daily_command: 'Daily Command',
  nightly_debrief: 'Nightly Debrief',
  weekly_war_room: 'Weekly War Room',
};

function ListBlock({ title, items }: { title: string; items: unknown[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="brief-block">
      <h4>{title}</h4>
      <ul>
        {items.map((x, i) => (
          <li key={i}>{textOf(x)}</li>
        ))}
      </ul>
    </div>
  );
}

function BriefingCard({ b }: { b: Briefing }) {
  const speech = useSpeech();
  const c = b.content ?? {};
  return (
    <Card
      className="briefing"
      title={
        <>
          {KIND_LABEL[b.kind] ?? b.kind} · {fmtDate(b.for_date)}
        </>
      }
      actions={
        <>
          {b.catch_up && (
            <Badge tone="warn" title="Generated after a missed scheduled run">
              catch-up
            </Badge>
          )}
          {b.spoken_summary && (
            <button type="button" className="btn btn-small" onClick={() => speech.speak(b.spoken_summary)} disabled={speech.prefs.muted} title={speech.prefs.muted ? 'Voice is muted' : undefined}>
              ▶ Play
            </button>
          )}
        </>
      }
    >
      {c.headline && <p className="headline">{c.headline}</p>}
      {c.mission && (
        <p>
          <span className="muted">Mission:</span> {c.mission}
        </p>
      )}
      <div className="brief-grid">
        <ListBlock title="Priorities" items={c.priorities} />
        <ListBlock title="Agenda" items={c.agenda} />
        <ListBlock title="Due" items={c.due} />
        <ListBlock title="Deadlines" items={c.deadlines} />
        <ListBlock title="Work windows" items={c.windows} />
        <ListBlock title="Conflicts" items={c.conflicts} />
        {c.sections?.map((s, i) => <ListBlock key={i} title={s.title} items={s.items} />)}
      </div>
      {c.obstacle && (
        <p>
          <span className="muted">Likely obstacle:</span> {c.obstacle}
        </p>
      )}
      {c.first_action && (
        <p>
          <span className="muted">First action:</span> <strong>{c.first_action}</strong>
        </p>
      )}
      {b.sources && Object.keys(b.sources).length > 0 && (
        <div className="sources small">
          {Object.entries(b.sources).map(([k, v]) =>
            typeof v === 'string' ? <SourceBadge key={k} name={k} info={{ status: v as SourceInfo['status'], last_sync: null, detail: null }} /> : <SourceBadge key={k} name={k} info={v} />,
          )}
        </div>
      )}
      <p className="muted small">
        Generated {fmtDateTime(b.created_at)}
        {b.generated_by ? ` by ${b.generated_by}` : ''}
      </p>
    </Card>
  );
}

function DebriefForm({ briefingId }: { briefingId?: string }) {
  const [completed, setCompleted] = useState('');
  const [changes, setChanges] = useState('');
  const [carry, setCarry] = useState('');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean);
  return (
    <Card title="Nightly debrief answers">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            await api.nightlyDebrief({ briefing_id: briefingId, answers: { completed: lines(completed), changes, carryover: lines(carry), notes } });
            setDone(true);
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <p className="hint">Local records update directly. Marking Todoist tasks complete still goes through approval.</p>
        <label className="field">
          <span>What did you complete? (one per line)</span>
          <textarea rows={3} value={completed} onChange={(e) => setCompleted(e.target.value)} />
        </label>
        <label className="field">
          <span>What changed?</span>
          <textarea rows={2} value={changes} onChange={(e) => setChanges(e.target.value)} />
        </label>
        <label className="field">
          <span>Carry over to tomorrow (one per line)</span>
          <textarea rows={2} value={carry} onChange={(e) => setCarry(e.target.value)} />
        </label>
        <label className="field">
          <span>Notes</span>
          <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
        <ErrorBox error={err} />
        {done && <p className="ok-text">Debrief saved. Any proposed task completions appear in the Action center.</p>}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? 'Saving…' : 'Submit debrief'}
        </button>
      </form>
    </Card>
  );
}

function PlanHistory() {
  const history = useAsync(() => api.planHistory(), []);
  return (
    <Async state={history} empty="No plan versions yet.">
      {(list) => (
        <ol className="stack plain">
          {list.map((p, i) => (
            <li key={`${p.version}-${i}`} className="card">
              <div className="row gap wrap">
                <strong>v{p.version}</strong>
                {p.date && <span>{fmtDate(p.date)}</span>}
                <span className="muted small">
                  by {p.author}
                  {p.created_at ? ` · ${fmtDateTime(p.created_at)}` : ''}
                </span>
              </div>
              <p>{p.mission}</p>
              {p.priorities.length > 0 && (
                <ol>
                  {p.priorities.map((x, j) => (
                    <li key={j}>{x}</li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ol>
      )}
    </Async>
  );
}

export function BriefingsPage() {
  const [tab, setTab] = useState<'briefings' | 'debrief' | 'plans'>('briefings');
  const [kind, setKind] = useState<BriefingKind | ''>('');
  const briefings = useAsync(() => api.briefings(kind || undefined, 20), [kind]);
  const [gen, setGen] = useState<BriefingKind | null>(null);
  const [genErr, setGenErr] = useState<unknown>(null);

  const generate = async (k: BriefingKind) => {
    setGen(k);
    setGenErr(null);
    try {
      const b = await api.generateBriefing(k);
      briefings.setData((l) => [b, ...(l ?? []).filter((x) => x.id !== b.id)]);
    } catch (e) {
      setGenErr(e);
    } finally {
      setGen(null);
    }
  };
  const latestNightly = briefings.data?.find((b) => b.kind === 'nightly_debrief');

  return (
    <div className="page">
      <header className="page-head">
        <h1>Briefings</h1>
      </header>
      <Tabs
        label="Briefing views"
        value={tab}
        onChange={setTab}
        tabs={[
          { key: 'briefings', label: 'Briefings' },
          { key: 'debrief', label: 'Nightly debrief' },
          { key: 'plans', label: 'Plan history' },
        ]}
      />
      {tab === 'briefings' && (
        <>
          <div className="toolbar">
            {(Object.keys(KIND_LABEL) as BriefingKind[]).map((k) => (
              <button key={k} type="button" className="btn" onClick={() => void generate(k)} disabled={gen !== null}>
                {gen === k ? 'Generating…' : `Generate ${KIND_LABEL[k]}`}
              </button>
            ))}
            <label className="inline small">
              Show
              <select value={kind} onChange={(e) => setKind(e.target.value as BriefingKind | '')}>
                <option value="">all</option>
                {(Object.keys(KIND_LABEL) as BriefingKind[]).map((k) => (
                  <option key={k} value={k}>
                    {KIND_LABEL[k]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <ErrorBox error={genErr} title="Generation failed" />
          <Async state={briefings} empty="No briefings yet. Generate one above, or wait for the schedule.">
            {(list) => (
              <div className="stack">
                {list.map((b) => (
                  <BriefingCard key={b.id} b={b} />
                ))}
              </div>
            )}
          </Async>
        </>
      )}
      {tab === 'debrief' && (
        <>
          {latestNightly ? <BriefingCard b={latestNightly} /> : <Empty>No nightly debrief generated yet — you can still answer below.</Empty>}
          <DebriefForm briefingId={latestNightly?.id} />
        </>
      )}
      {tab === 'plans' && <PlanHistory />}
    </div>
  );
}
