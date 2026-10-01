import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Briefing, Mission, MissionSchedule, MissionTemplate, MissionsResponse, Schedule } from '../api/types';
import { PageHead } from '../components/PageHead';
import { Async, Badge, Card, ErrorBox, Toggle } from '../components/ui';
import { CalendarTimeline } from '../components/Visuals';
import { useAsync } from '../lib/hooks';
import { useConversation, useSpeech } from '../state/app';

const FAMILY_LABEL: Record<string, string> = {
  chief: 'Planning', calendar: 'Calendar', tasks: 'Tasks', weather: 'Weather', school_gta: 'School & Canvas',
  briefing: 'Briefings', memory: 'Memory', knowledge: 'Documents', email: 'Email', messaging: 'Slack', notes: 'Notion',
  life: 'Music', home: 'Smart home', travel: 'Travel', finance: 'Money', github: 'GitHub', local_operator: 'Workspace files',
  mcp: 'MCP servers', research: 'Web research',
};
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const STATUS: Record<string, { label: string; tone: 'ok' | 'neutral' | 'bad' | 'warn' }> = {
  done: { label: 'reported', tone: 'ok' },
  nothing_new: { label: 'nothing new', tone: 'neutral' },
  failed: { label: 'failed', tone: 'bad' },
  skipped_budget: { label: 'budget reached', tone: 'warn' },
  running: { label: 'running', tone: 'neutral' },
};

const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : '—');

function describe(s: MissionSchedule): string {
  if (s.kind === 'interval') return s.minutes! % 60 === 0 ? `every ${s.minutes! / 60} h` : `every ${s.minutes} min`;
  const t = new Date(`2000-01-01T${s.at}:00`).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return s.kind === 'daily' ? `daily at ${t}` : `${(s.days ?? []).map((d) => DAYS[d]).join(', ')} at ${t}`;
}

/* Signature element: the next 24 hours as a rail, with every mission and briefing run placed on it. */
function TimeRail({ missions, schedules }: { missions: Mission[]; schedules: Schedule[] }) {
  const now = Date.now();
  const span = 24 * 3600 * 1000;
  const marks = [
    ...missions.filter((m) => m.enabled && m.next_run_at).map((m) => ({ id: m.id, label: m.name, at: Date.parse(m.next_run_at!), kind: 'mission' })),
    ...schedules.filter((s) => s.enabled && s.next_run_at).map((s) => ({ id: s.id, label: s.label, at: Date.parse(s.next_run_at!), kind: 'briefing' })),
  ].filter((m) => m.at - now < span && m.at >= now - 60000);
  const hours = Array.from({ length: 5 }, (_, i) => now + (i * span) / 4);
  return (
    <div className="time-rail" aria-label="Runs in the next 24 hours">
      <div className="rail-line">
        {marks.length === 0 && <span className="rail-empty">Nothing scheduled in the next 24 hours.</span>}
        {marks.map((m) => (
          <span key={m.id} className={`rail-mark ${m.kind}`} style={{ left: `${((m.at - now) / span) * 100}%` }} title={`${m.label}: ${fmtTime(new Date(m.at).toISOString())}`}>
            <i aria-hidden="true" />
            <span className="rail-label">{m.label}</span>
          </span>
        ))}
      </div>
      <div className="rail-ticks" aria-hidden="true">
        {hours.map((h, i) => (
          <span key={i}>{i === 0 ? 'now' : new Date(h).toLocaleTimeString([], { hour: 'numeric' })}</span>
        ))}
      </div>
    </div>
  );
}

function TodayBriefing() {
  const briefing = useAsync(() => api.briefings('daily_command', 1), []);
  const today = useAsync(() => api.today(), []);
  const { speak } = useSpeech();
  const { send } = useConversation();
  const nav = useNavigate();
  const ask = (text: string) => {
    nav('/console');
    void send({ text, channel: 'typed', captured_at: new Date().toISOString(), speech_playing: false });
  };
  const b: Briefing | undefined = briefing.data?.[0];
  return (
    <section className="briefing-hero" aria-label="Today's briefing">
      <div className="bh-text">
        <h2>{b?.content.headline || (briefing.loading ? 'Loading today’s briefing…' : 'No briefing yet today')}</h2>
        {b?.content.mission && <p className="bh-mission">{b.content.mission}</p>}
        {b?.content.first_action && (
          <p>
            Start with: <strong>{b.content.first_action}</strong>
          </p>
        )}
        <div className="row gap wrap">
          {b?.spoken_summary && (
            <button type="button" className="btn btn-primary" onClick={() => speak(b.spoken_summary)}>
              Play briefing
            </button>
          )}
          <button type="button" className="btn" onClick={() => void api.generateBriefing('daily_command', true).then(() => briefing.reload())}>
            {b ? 'Refresh' : 'Generate'} Daily Command
          </button>
          <button type="button" className="btn" onClick={() => ask('Talk me through today: what matters most and what should I do first?')}>
            Ask about today
          </button>
          <button type="button" className="btn" onClick={() => ask("Let's do my nightly debrief. Ask me what I got done, what changed and what carries over, one question at a time.")}>
            Start nightly debrief
          </button>
        </div>
        {b && (
          <details className="small muted">
            <summary>Sources</summary>
            Generated {new Date(b.created_at).toLocaleString()} by {b.generated_by ?? 'JARVIS'}.
          </details>
        )}
      </div>
      <div className="bh-cal">{today.data?.calendar_visual && <CalendarTimeline v={today.data.calendar_visual} />}</div>
    </section>
  );
}

function MissionCard({ m, onChanged }: { m: Mission; onChanged: () => void }) {
  const [err, setErr] = useState<unknown>(null);
  const [queued, setQueued] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (x) {
      setErr(x);
    }
  };
  const st = m.last_status ? STATUS[m.last_status] : null;
  return (
    <li className={`mission ${m.enabled ? '' : 'paused'}`} data-testid={`mission-${m.name}`}>
      <div className="mission-head">
        <h3>{m.name}</h3>
        {!m.enabled && <Badge tone="neutral">paused</Badge>}
        {st && <Badge tone={st.tone}>{st.label}</Badge>}
      </div>
      <p className="mission-goal">{m.goal}</p>
      <p className="small muted">
        Runs {describe(m.schedule)}. Next: {m.enabled ? fmtTime(m.next_run_at) : 'paused'}. Uses{' '}
        {(m.families.length ? m.families : ['planning tools']).map((f) => FAMILY_LABEL[f] ?? f).join(', ')}.{' '}
        {m.allow_proposals ? 'May propose changes.' : 'Reports only.'} ${m.month_spend.toFixed(2)} of ${m.monthly_cost_usd.toFixed(2)} this month.
      </p>
      {m.last_summary && <blockquote className="mission-last">{m.last_summary}</blockquote>}
      <div className="row gap wrap">
        <button type="button" className="btn btn-small btn-primary" onClick={() => void act(async () => { await api.runMission(m.id); setQueued(true); })}>
          Run now
        </button>
        <button type="button" className="btn btn-small" onClick={() => void act(() => api.missionEnabled(m.id, !m.enabled))}>
          {m.enabled ? 'Pause' : 'Resume'}
        </button>
        <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => window.confirm(`Delete mission “${m.name}”?`) && void act(() => api.deleteMission(m.id))}>
          Delete
        </button>
        {queued && <span className="small muted">Queued — results arrive in Notifications.</span>}
      </div>
      {m.runs.length > 0 && (
        <details className="small">
          <summary>Recent runs</summary>
          <ul className="plain run-list">
            {m.runs.map((r) => (
              <li key={r.id}>
                <span className="mono">{fmtTime(r.started_at)}</span> <Badge tone={STATUS[r.status]?.tone ?? 'neutral'}>{STATUS[r.status]?.label ?? r.status}</Badge>{' '}
                {r.summary && <span>{r.summary.slice(0, 160)}</span>} <span className="muted">${r.cost_usd.toFixed(3)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <ErrorBox error={err} />
    </li>
  );
}

function NewMission({ data, onDone, initial }: { data: MissionsResponse; onDone: () => void; initial?: MissionTemplate | null }) {
  const [m, setM] = useState<Partial<Mission>>(() => ({
    name: '', goal: '', schedule: { kind: 'daily', at: '07:00' }, families: [], allow_proposals: false, output: 'quiet',
    max_cost_usd: 0.25, monthly_cost_usd: 5, ...initial,
  }));
  const [err, setErr] = useState<unknown>(null);
  const sch = m.schedule!;
  const set = (p: Partial<Mission>) => setM({ ...m, ...p });
  return (
    <form
      className="mission-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setErr(null);
        try {
          await api.createMission(m);
          onDone();
        } catch (x) {
          setErr(x);
        }
      }}
    >
      <label className="field">
        <span>Name</span>
        <input value={m.name ?? ''} onChange={(e) => set({ name: e.target.value })} placeholder="Inbox watch" />
      </label>
      <label className="field">
        <span>What should JARVIS do? (in your words)</span>
        <textarea rows={3} value={m.goal ?? ''} onChange={(e) => set({ goal: e.target.value })} placeholder="Check my inbox for anything from my advisor and tell me who needs a reply." />
      </label>
      <div className="row gap wrap">
        <label className="field">
          <span>When</span>
          <select value={sch.kind} onChange={(e) => set({ schedule: e.target.value === 'interval' ? { kind: 'interval', minutes: 120 } : { kind: e.target.value as 'daily' | 'weekly', at: sch.at ?? '07:00', days: [0] } })}>
            <option value="daily">Every day</option>
            <option value="weekly">On certain days</option>
            <option value="interval">Every few hours</option>
          </select>
        </label>
        {sch.kind === 'interval' ? (
          <label className="field">
            <span>Every (minutes)</span>
            <input type="number" min={15} value={sch.minutes ?? 120} onChange={(e) => set({ schedule: { kind: 'interval', minutes: Number(e.target.value) } })} />
          </label>
        ) : (
          <label className="field">
            <span>At</span>
            <input type="time" value={sch.at ?? '07:00'} onChange={(e) => set({ schedule: { ...sch, at: e.target.value } })} />
          </label>
        )}
      </div>
      {sch.kind === 'weekly' && (
        <fieldset className="chips" aria-label="Days">
          {DAYS.map((d, i) => (
            <label key={d} className="chip">
              <input
                type="checkbox"
                checked={(sch.days ?? []).includes(i)}
                onChange={(e) => set({ schedule: { ...sch, days: e.target.checked ? [...(sch.days ?? []), i] : (sch.days ?? []).filter((x) => x !== i) } })}
              />{' '}
              {d}
            </label>
          ))}
        </fieldset>
      )}
      <fieldset className="chips" aria-label="Apps this mission may use">
        <legend className="small muted">Apps it may use</legend>
        {data.families.filter((f) => f !== 'chief').map((f) => (
          <label key={f} className="chip">
            <input
              type="checkbox"
              checked={(m.families ?? []).includes(f)}
              onChange={(e) => set({ families: e.target.checked ? [...(m.families ?? []), f] : (m.families ?? []).filter((x) => x !== f) })}
            />{' '}
            {FAMILY_LABEL[f] ?? f}
          </label>
        ))}
      </fieldset>
      <Toggle label="May propose changes (they go to your approvals)" checked={!!m.allow_proposals} onChange={(v) => set({ allow_proposals: v })} />
      <Toggle label="Only notify me when it finds something" checked={m.output === 'quiet'} onChange={(v) => set({ output: v ? 'quiet' : 'notify' })} />
      <div className="row gap wrap">
        <label className="field">
          <span>Max per run ($)</span>
          <input type="number" step="0.05" min={0.01} max={5} value={m.max_cost_usd} onChange={(e) => set({ max_cost_usd: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span>Max per month ($)</span>
          <input type="number" step="0.5" min={0.1} max={100} value={m.monthly_cost_usd} onChange={(e) => set({ monthly_cost_usd: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span>Quiet from</span>
          <input type="time" value={m.quiet_start ?? ''} onChange={(e) => set({ quiet_start: e.target.value || null })} />
        </label>
        <label className="field">
          <span>until</span>
          <input type="time" value={m.quiet_end ?? ''} onChange={(e) => set({ quiet_end: e.target.value || null })} />
        </label>
      </div>
      <button type="submit" className="btn btn-primary">
        Create mission
      </button>
      <ErrorBox error={err} />
    </form>
  );
}

function ArchiveAndHistory() {
  const archive = useAsync(() => api.briefings(undefined, 12), []);
  const plans = useAsync(() => api.planHistory(), []);
  return (
    <details className="card archive">
      <summary>Briefing archive and plan history</summary>
      <Async state={archive} empty="No briefings yet.">
        {(list) => (
          <ul className="plain run-list">
            {list.map((b) => (
              <li key={b.id}>
                <span className="mono">{b.for_date}</span> {b.kind.replace(/_/g, ' ')}: {b.content.headline ?? b.content.mission ?? ''}
              </li>
            ))}
          </ul>
        )}
      </Async>
      <Async state={plans} empty="No saved plans.">
        {(list) => (
          <ul className="plain run-list">
            {list.map((p) => (
              <li key={`${p.version}-${p.date ?? ''}`}>
                v{p.version}: {p.mission}
              </li>
            ))}
          </ul>
        )}
      </Async>
    </details>
  );
}

export function MissionsPage() {
  const data = useAsync(() => api.missions(), []);
  const schedules = useAsync(() => api.schedules(), []);
  const [template, setTemplate] = useState<MissionTemplate | null | undefined>(undefined);
  const missions = useMemo(() => data.data?.missions ?? [], [data.data]);
  return (
    <div className="page missions-page">
      <PageHead title="Missions" guide="missions">
        Standing goals JARVIS works on in the background, plus your daily briefings.
      </PageHead>
      <TodayBriefing />
      <TimeRail missions={missions} schedules={schedules.data ?? []} />
      <Card title="Your missions">
        <Async state={data}>
          {(d) => (
            <>
              {d.missions.length === 0 && <p className="muted">No missions yet. Start from one of these or write your own.</p>}
              <ul className="plain missions">
                {d.missions.map((m) => (
                  <MissionCard key={m.id} m={m} onChanged={() => void data.reload()} />
                ))}
              </ul>
              <div className="row gap wrap templates">
                {d.templates.map((t) => (
                  <button key={t.key} type="button" className="btn btn-small" onClick={() => setTemplate(t)}>
                    + {t.name}
                  </button>
                ))}
                <button type="button" className="btn btn-small" onClick={() => setTemplate(null)}>
                  + Blank mission
                </button>
              </div>
              {template !== undefined && (
                <NewMission
                  key={template?.key ?? 'blank'}
                  data={d}
                  initial={template}
                  onDone={() => {
                    setTemplate(undefined);
                    void data.reload();
                  }}
                />
              )}
            </>
          )}
        </Async>
      </Card>
      <ArchiveAndHistory />
    </div>
  );
}
