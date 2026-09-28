export const localTz = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone;

const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function fmtDateTime(iso: string | null | undefined, tz?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  try {
    return d.toLocaleString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZone: tz,
      timeZoneName: tz ? 'short' : undefined,
    });
  } catch {
    return d.toLocaleString();
  }
}

export function fmtTime(iso: string | null | undefined, tz?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  try {
    return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', timeZone: tz });
  } catch {
    return d.toLocaleTimeString();
  }
}

export function fmtDate(value: string | null | undefined): string {
  if (!value) return '—';
  if (DATE_RE.test(value)) {
    const [y, m, d] = value.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
      weekday: 'long',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    });
  }
  return fmtDateTime(value);
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = (t - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (abs < 60) return rtf.format(Math.round(diff), 'second');
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
  return rtf.format(Math.round(diff / 86400), 'day');
}

export function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `$${n.toFixed(2)}`;
}

export function humanKey(k: string): string {
  const map: Record<string, string> = {
    due_date: 'Due date',
    due_datetime: 'Due (date & time)',
    due_string: 'Due',
    tz: 'Timezone',
    timezone: 'Timezone',
    duration_minutes: 'Duration',
    duration: 'Duration',
    project_id: 'Project',
    calendar_id: 'Calendar',
    start: 'Starts',
    end: 'Ends',
    content: 'Title',
    summary: 'Title',
  };
  if (map[k]) return map[k];
  const s = k.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Human-readable rendering of an action argument value. */
export function humanValue(key: string, v: unknown, tz?: string): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  if (typeof v === 'number') {
    if (/duration/.test(key)) return v >= 60 ? `${Math.floor(v / 60)} h ${v % 60 ? `${v % 60} min` : ''}`.trim() : `${v} min`;
    if (key === 'priority') return `P${5 - v > 0 ? 5 - v : v}`;
    return String(v);
  }
  if (typeof v === 'string') {
    if (ISO_RE.test(v)) return fmtDateTime(v, tz);
    if (DATE_RE.test(v)) return fmtDate(v);
    return v;
  }
  if (Array.isArray(v)) return v.map((x) => humanValue(key, x, tz)).join(', ');
  return JSON.stringify(v);
}

export function textOf(x: unknown): string {
  if (x === null || x === undefined) return '';
  if (typeof x === 'string') return x;
  if (typeof x === 'number' || typeof x === 'boolean') return String(x);
  if (typeof x === 'object') {
    const o = x as Record<string, unknown>;
    for (const k of ['text', 'title', 'content', 'summary', 'name', 'label', 'message']) {
      if (typeof o[k] === 'string') {
        const extra = ['due', 'start', 'time', 'when', 'due_at']
          .map((kk) => (typeof o[kk] === 'string' ? humanValue(kk, o[kk]) : ''))
          .filter(Boolean)[0];
        return extra ? `${o[k] as string} — ${extra}` : (o[k] as string);
      }
    }
    return JSON.stringify(x);
  }
  return String(x);
}
