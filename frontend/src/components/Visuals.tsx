import type { CSSProperties } from 'react';

/* Visual cards for data Jarvis actually read (never decorative/fake). */

export interface CalEvent {
  summary: string;
  start?: string;
  end?: string;
  all_day: boolean;
  busy?: boolean;
  calendar?: string | null;
}
export interface CalendarVisual {
  type: 'calendar';
  tz: string;
  status?: string;
  detail?: string | null;
  days: { date: string; label: string; events: CalEvent[]; windows: { start: string; end: string }[] }[];
}
export interface WeatherDay {
  date: string;
  summary: string;
  high: number | null;
  low: number | null;
  rain_chance: number | null;
  wind: number | null;
  code?: number | null;
}
export interface WeatherVisual {
  type: 'weather';
  location: string;
  focus: string | null;
  current?: { temp: number | null; feels_like: number | null; code: number | null } | null;
  days: WeatherDay[];
  disabled?: boolean;
}
export interface TasksVisual {
  type: 'tasks';
  title: string;
  tasks: { content: string; due?: string | null; priority?: number | null }[];
}
export interface SlotsVisual {
  type: 'slots';
  date: string;
  label: string;
  minutes: number;
  slots: { start: string; end: string }[];
}
export interface InboxVisual {
  type: 'inbox';
  counts: Record<string, number>;
  messages: {
    ref: string;
    mailbox: string;
    category: 'needs_reply' | 'waiting' | 'fyi' | 'bulk';
    from_name: string;
    subject: string;
    snippet: string;
    date: string | null;
    unread: boolean;
    web_url: string | null;
  }[];
}
export interface ScreenshotVisual {
  type: 'screenshot';
  shot_id: string;
  title?: string | null;
  url?: string | null;
}
export type Visual = CalendarVisual | WeatherVisual | TasksVisual | SlotsVisual | InboxVisual | ScreenshotVisual;

export const MAIL_CATEGORY: Record<string, string> = {
  needs_reply: 'Needs reply',
  waiting: 'Waiting',
  fyi: 'FYI',
  bulk: 'Bulk',
};

export function relTime(iso: string | null): string {
  if (!iso) return '';
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h`;
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

const hm = (iso: string) => {
  const d = new Date(iso);
  return d.getHours() + d.getMinutes() / 60;
};
const fmt = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const hourLabel = (h: number) => (h === 0 || h === 24 ? '12a' : h === 12 ? '12p' : h > 12 ? `${h - 12}p` : `${h}a`);

function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Lay out overlapping events side by side. */
function columns(evs: CalEvent[]): { ev: CalEvent; col: number; cols: number }[] {
  const timed = evs.filter((e) => !e.all_day && e.start && e.end).sort((a, b) => hm(a.start!) - hm(b.start!));
  const out: { ev: CalEvent; col: number; cols: number }[] = [];
  let group: { ev: CalEvent; col: number; cols: number }[] = [];
  let groupEnd = -1;
  const flush = () => {
    const n = Math.max(1, ...group.map((g) => g.col + 1));
    group.forEach((g) => (g.cols = n));
    out.push(...group);
    group = [];
  };
  for (const ev of timed) {
    const s = hm(ev.start!);
    const e = Math.max(s + 0.25, hm(ev.end!) || 24);
    if (s >= groupEnd && group.length) flush();
    const used = new Set(group.filter((g) => hm(g.ev.end!) > s).map((g) => g.col));
    let col = 0;
    while (used.has(col)) col++;
    group.push({ ev, col, cols: 1 });
    groupEnd = Math.max(groupEnd, e);
  }
  if (group.length) flush();
  return out;
}

export function CalendarTimeline({ v, compact = false, bare = false }: { v: CalendarVisual; compact?: boolean; bare?: boolean }) {
  if (v.status && !['ok', 'partial'].includes(v.status)) {
    return (
      <div className="viz viz-cal">
        <div className="viz-head">Calendar</div>
        <p className="muted small">Calendar {v.status.replace('_', ' ')}{v.detail ? ` — ${v.detail}` : ''}</p>
      </div>
    );
  }
  const all = v.days.flatMap((d) => d.events.filter((e) => !e.all_day));
  let start = 7;
  let end = 22;
  for (const e of all) {
    start = Math.min(start, Math.floor(hm(e.start!)));
    end = Math.max(end, Math.ceil(hm(e.end!) || 24));
  }
  const pxh = compact ? 30 : 38;
  const height = (end - start) * pxh;
  const today = localToday();
  const nowH = new Date().getHours() + new Date().getMinutes() / 60;
  return (
    <div className={bare ? 'viz-cal viz-bare' : 'viz viz-cal'} data-testid="viz-calendar">
      <div className="viz-head" hidden={bare}>
        Calendar
        <span className="muted small">{v.days.length === 1 ? v.days[0].label : `${v.days[0].label} – ${v.days[v.days.length - 1].label}`}</span>
      </div>
      {v.days.some((d) => d.events.some((e) => e.all_day)) && (
        <div className="cal-grid cal-allday-row" style={{ ['--cols' as string]: v.days.length } as CSSProperties}>
          <span className="cal-allday-tag">all day</span>
          {v.days.map((d) => (
            <div key={d.date} className="cal-allday">
              {d.events
                .filter((e) => e.all_day)
                .map((e, i) => (
                  <span key={i} className="chip">
                    {e.summary}
                  </span>
                ))}
            </div>
          ))}
        </div>
      )}
      <div className="cal-grid" style={{ ['--cols' as string]: v.days.length } as CSSProperties}>
        <div className="cal-hours" style={{ height, marginTop: v.days.length > 1 ? 22 : 0 }}>
          {Array.from({ length: end - start + 1 }, (_, i) => (
            <span key={i} style={{ top: i * pxh }}>
              {hourLabel(start + i)}
            </span>
          ))}
        </div>
        {v.days.map((d) => {
          return (
            <div key={d.date} className="cal-day">
              {v.days.length > 1 && <div className="cal-day-label">{d.label}</div>}
              <div className="cal-col" style={{ height }}>
                {Array.from({ length: end - start }, (_, i) => (
                  <div key={i} className="cal-line" style={{ top: i * pxh }} />
                ))}
                {d.windows.map((w, i) => {
                  const s = Math.max(start, hm(w.start));
                  const e = Math.min(end, hm(w.end) || 24);
                  return e > s ? (
                    <div key={i} className="cal-free" style={{ top: (s - start) * pxh, height: (e - s) * pxh }} title="Open time">
                      {e - s >= 1 && <span>open</span>}
                    </div>
                  ) : null;
                })}
                {columns(d.events).map(({ ev, col, cols }, i) => {
                  const s = Math.max(start, hm(ev.start!));
                  const e = Math.min(end, Math.max(s + 0.25, hm(ev.end!) || 24));
                  return (
                    <div
                      key={i}
                      className={`cal-ev ${ev.busy === false ? 'free' : ''}`}
                      style={{ top: (s - start) * pxh, height: Math.max(18, (e - s) * pxh - 2), left: `${(col / cols) * 100}%`, width: `${100 / cols}%` }}
                      title={`${ev.summary} · ${fmt(ev.start!)}–${fmt(ev.end!)}`}
                    >
                      <strong>{ev.summary}</strong>
                      <span>
                        {fmt(ev.start!)}–{fmt(ev.end!)}
                      </span>
                    </div>
                  );
                })}
                {d.date === today && nowH >= start && nowH <= end && <div className="cal-now" style={{ top: (nowH - start) * pxh }} />}
                {d.events.filter((e) => !e.all_day).length === 0 && <div className="cal-empty">Nothing scheduled</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WxIcon({ code, size = 44 }: { code?: number | null; size?: number }) {
  const c = code ?? 1;
  const kind = c === 0 || c === 1 ? 'sun' : c <= 3 ? 'cloudsun' : c <= 48 ? 'fog' : c >= 95 ? 'storm' : (c >= 71 && c <= 77) || c >= 85 ? 'snow' : 'rain';
  const stroke = 'currentColor';
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" stroke={stroke} strokeWidth="2.2" strokeLinecap="round" aria-hidden="true" className={`wx wx-${kind}`}>
      {(kind === 'sun' || kind === 'cloudsun') && (
        <g className="wx-sun">
          <circle cx={kind === 'sun' ? 24 : 18} cy={kind === 'sun' ? 24 : 18} r={kind === 'sun' ? 8 : 6} />
          {[0, 45, 90, 135, 180, 225, 270, 315].map((a) => {
            const cx = kind === 'sun' ? 24 : 18;
            const r1 = kind === 'sun' ? 13 : 10;
            const r2 = kind === 'sun' ? 17 : 13;
            const rad = (a * Math.PI) / 180;
            return <line key={a} x1={cx + r1 * Math.cos(rad)} y1={cx + r1 * Math.sin(rad)} x2={cx + r2 * Math.cos(rad)} y2={cx + r2 * Math.sin(rad)} />;
          })}
        </g>
      )}
      {kind !== 'sun' && <path d="M14 34h20a7 7 0 0 0 0-14 10 10 0 0 0-19-2 7 7 0 0 0-1 16z" className="wx-cloud" />}
      {kind === 'rain' && <path d="M18 38l-2 5M25 38l-2 5M32 38l-2 5" className="wx-drop" />}
      {kind === 'storm' && <path d="M25 36l-4 6h5l-3 5" className="wx-bolt" />}
      {kind === 'snow' && <path d="M18 40h.01M25 42h.01M32 40h.01" strokeWidth="4" />}
      {kind === 'fog' && <path d="M12 40h24M16 44h18" />}
    </svg>
  );
}

export function WeatherCard({ v }: { v: WeatherVisual }) {
  if (v.disabled || !v.days?.length) return null;
  const focus = v.days.find((d) => d.date === v.focus) ?? v.days[0];
  const dayName = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: 'short' });
  const isToday = focus.date === localToday();
  return (
    <div className="viz viz-wx" data-testid="viz-weather">
      <div className="viz-head">
        Weather <span className="muted small">{v.location}</span>
      </div>
      <div className="wx-main">
        <WxIcon code={focus.code} size={64} />
        <div>
          <div className="wx-temp">
            {isToday && v.current?.temp != null ? Math.round(v.current.temp) : Math.round(focus.high ?? 0)}°
            <span className="muted small"> {isToday && v.current?.temp != null ? 'now' : 'high'}</span>
          </div>
          <div className="wx-sum">
            {isToday ? 'Today' : dayName(focus.date)} · {focus.summary}
          </div>
          <div className="wx-meta">
            H {Math.round(focus.high ?? 0)}° · L {Math.round(focus.low ?? 0)}° · Rain {focus.rain_chance ?? 0}% · Wind {Math.round(focus.wind ?? 0)} mph
          </div>
        </div>
      </div>
      <div className="wx-strip">
        {v.days.map((d) => (
          <div key={d.date} className={`wx-day ${d.date === focus.date ? 'on' : ''}`}>
            <span>{d.date === localToday() ? 'Today' : dayName(d.date)}</span>
            <WxIcon code={d.code} size={24} />
            <span className="wx-hl">
              {Math.round(d.high ?? 0)}° <span className="muted">{Math.round(d.low ?? 0)}°</span>
            </span>
            {(d.rain_chance ?? 0) >= 30 && <span className="wx-rain">{d.rain_chance}%</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

export function TasksCard({ v }: { v: TasksVisual }) {
  const today = localToday();
  return (
    <div className="viz viz-tasks" data-testid="viz-tasks">
      <div className="viz-head">{v.title}</div>
      <ul>
        {v.tasks.map((t, i) => {
          const due = t.due ? t.due.slice(0, 10) : null;
          const tone = !due ? '' : due < today ? 'late' : due === today ? 'today' : 'later';
          return (
            <li key={i}>
              <span className={`prio p${t.priority ?? 1}`} aria-hidden="true" />
              <span className="t">{t.content}</span>
              {due && <span className={`due ${tone}`}>{tone === 'late' ? 'overdue' : tone === 'today' ? 'today' : new Date(`${due}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' })}</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function SlotsCard({ v }: { v: SlotsVisual }) {
  return (
    <div className="viz viz-slots">
      <div className="viz-head">
        Open {v.minutes}-min slots <span className="muted small">{v.label}</span>
      </div>
      <div className="slot-chips">
        {v.slots.map((s, i) => (
          <span key={i} className="chip">
            {fmt(s.start)}–{fmt(s.end)}
          </span>
        ))}
      </div>
    </div>
  );
}

export function InboxCard({ v }: { v: InboxVisual }) {
  const needs = v.counts.needs_reply ?? 0;
  return (
    <div className="viz viz-inbox" data-testid="viz-inbox">
      <div className="viz-head">
        Inbox{' '}
        <span className="muted small">
          {needs ? `${needs} need${needs === 1 ? 's' : ''} a reply` : 'nothing waiting on you'}
        </span>
      </div>
      <ul>
        {v.messages
          .filter((m) => m.category !== 'bulk')
          .slice(0, 6)
          .map((m) => (
            <li key={m.ref} className={`cat-${m.category}${m.unread ? ' unread' : ''}`}>
              <span className="who">{m.from_name}</span>
              <span className="subj">{m.subject}</span>
              <span className={`tag tag-${m.category}`}>{MAIL_CATEGORY[m.category]}</span>
              <span className="when mono">{relTime(m.date)}</span>
            </li>
          ))}
      </ul>
    </div>
  );
}

export function Visuals({ items }: { items?: Visual[] | null }) {
  if (!items?.length) return null;
  const order = { weather: 0, calendar: 1, slots: 2, inbox: 3, tasks: 4 } as Record<string, number>;
  return (
    <div className="viz-wrap">
      {[...items]
        .sort((a, b) => (order[a.type] ?? 9) - (order[b.type] ?? 9))
        .map((v, i) =>
          v.type === 'calendar' ? (
            <CalendarTimeline key={i} v={v} />
          ) : v.type === 'weather' ? (
            <WeatherCard key={i} v={v} />
          ) : v.type === 'tasks' ? (
            <TasksCard key={i} v={v} />
          ) : v.type === 'slots' ? (
            <SlotsCard key={i} v={v} />
          ) : v.type === 'inbox' ? (
            <InboxCard key={i} v={v} />
          ) : v.type === 'screenshot' ? (
            <figure key={i} className="viz viz-shot">
              <div className="viz-head">
                {v.title || 'Sandbox browser'} {v.url && <span className="muted small">{v.url}</span>}
              </div>
              <img src={`/api/sandbox/shots/${v.shot_id}`} alt={`Screenshot of ${v.title || v.url || 'the page'}`} />
            </figure>
          ) : null,
        )}
    </div>
  );
}
