import { useId, useState, type ReactNode } from 'react';
import { ApiError, errorMessage } from '../api/client';
import type { SourceInfo } from '../api/types';
import { fmtRelative } from '../lib/format';

export type Tone = 'neutral' | 'ok' | 'warn' | 'bad' | 'info' | 'accent';

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

const SOURCE_LABEL: Record<string, { tone: Tone; label: string }> = {
  ok: { tone: 'ok', label: 'OK' },
  stale: { tone: 'warn', label: 'Stale' },
  unavailable: { tone: 'bad', label: 'Unavailable' },
  not_connected: { tone: 'neutral', label: 'Not connected' },
};

export function SourceBadge({ name, info }: { name: string; info: SourceInfo | undefined }) {
  const s = info ? SOURCE_LABEL[info.status] ?? { tone: 'warn' as Tone, label: info.status } : { tone: 'bad' as Tone, label: 'No status' };
  const detail = [info?.detail, info?.last_sync ? `last sync ${fmtRelative(info.last_sync)}` : null].filter(Boolean).join(' · ');
  return (
    <div className={`source source-${info?.status ?? 'missing'}`} data-testid={`source-${name}`} title={detail || undefined}>
      <span className="source-name">{name}</span>
      <Badge tone={s.tone}>{s.label}</Badge>
      {detail && <span className="source-detail">{detail}</span>}
    </div>
  );
}

const STATE_TONE: Record<string, Tone> = {
  verified: 'ok',
  ok: 'ok',
  active: 'ok',
  connected: 'ok',
  approved: 'info',
  queued: 'info',
  executing: 'info',
  proposed: 'accent',
  partial: 'warn',
  unknown: 'warn',
  conflict: 'warn',
  paused: 'warn',
  stale: 'warn',
  warn: 'warn',
  pending: 'neutral',
  failed: 'bad',
  fail: 'bad',
  error: 'bad',
  rejected: 'neutral',
  expired: 'neutral',
  superseded: 'neutral',
  cancelled: 'neutral',
  skipped: 'neutral',
  forgotten: 'neutral',
};
export function StateBadge({ state }: { state: string | null | undefined }) {
  const s = state ?? 'unknown';
  return <Badge tone={STATE_TONE[s] ?? 'neutral'}>{s.replace(/_/g, ' ')}</Badge>;
}

export function Card({ title, actions, children, className = '', id }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  return (
    <section className={`card ${className}`} id={id} aria-label={typeof title === 'string' ? title : undefined}>
      {(title || actions) && (
        <header className="card-head">
          {title && <h2 className="card-title">{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <p className="loading" role="status">
      {label}
    </p>
  );
}

export function ErrorBox({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  if (!error) return null;
  const msg = errorMessage(error);
  const notImpl = error instanceof ApiError && (error.status === 404 || error.status === 501);
  return (
    <div className="error-box" role="alert">
      <strong>{title ?? (notImpl ? 'Not available on this server' : 'Something went wrong')}</strong>
      <span>{msg}</span>
      {onRetry && (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Wraps an async-loaded region with loading / error / content. */
export function Async<T>({ state, children, empty, isEmpty }: {
  state: { data: T | undefined; error: unknown; loading: boolean; reload: () => Promise<void> };
  children: (d: T) => ReactNode;
  empty?: ReactNode;
  isEmpty?: (d: T) => boolean;
}) {
  if (state.error && state.data === undefined) return <ErrorBox error={state.error} onRetry={() => void state.reload()} />;
  if (state.data === undefined) return <Loading />;
  const d = state.data;
  if (empty && (isEmpty ? isEmpty(d) : Array.isArray(d) && d.length === 0)) return <Empty>{empty}</Empty>;
  return (
    <>
      {state.error ? <ErrorBox error={state.error} onRetry={() => void state.reload()} title="Refresh failed (showing last data)" /> : null}
      {children(d)}
    </>
  );
}

/** Button that is disabled with a visible, truthful reason. */
export function ReasonButton({ reason, className = 'btn', children, ...rest }: { reason: string | null } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const id = useId();
  return (
    <span className="reason-wrap">
      <button {...rest} className={className} disabled={!!reason || rest.disabled} aria-describedby={reason ? id : undefined}>
        {children}
      </button>
      {reason && (
        <span id={id} className="reason">
          {reason}
        </span>
      )}
    </span>
  );
}

export function Tabs<K extends string>({ tabs, value, onChange, label }: { tabs: { key: K; label: ReactNode }[]; value: K; onChange: (k: K) => void; label: string }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.key}
          type="button"
          role="tab"
          aria-selected={value === t.key}
          className={`tab ${value === t.key ? 'tab-active' : ''}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** Secret entry: value is never pre-filled and is cleared after submit. */
export function SecretForm({ label, placeholder, submitLabel, onSubmit, hint, type = 'password' }: {
  label: string;
  placeholder?: string;
  submitLabel: string;
  onSubmit: (value: string) => Promise<void>;
  hint?: ReactNode;
  type?: 'password' | 'url';
}) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [ok, setOk] = useState(false);
  const id = useId();
  return (
    <form
      className="form-row"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        setOk(false);
        try {
          await onSubmit(value.trim());
          setValue('');
          setOk(true);
        } catch (x) {
          setErr(x);
        } finally {
          setBusy(false);
        }
      }}
    >
      <label htmlFor={id}>{label}</label>
      <div className="inline">
        <input
          id={id}
          type={type === 'url' ? 'password' : 'password'}
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <ReasonButton className="btn btn-primary" reason={value.trim() ? null : busy ? null : 'Enter a value first'} disabled={busy} type="submit">
          {busy ? 'Saving…' : submitLabel}
        </ReasonButton>
      </div>
      {hint && <p className="hint">{hint}</p>}
      {ok && <p className="ok-text">Saved. The value is stored server-side and will not be shown again.</p>}
      <ErrorBox error={err} />
    </form>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], i) => (
        <div key={i} className="kv-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Toggle({ checked, onChange, label, disabled, description }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean; description?: ReactNode }) {
  const id = useId();
  return (
    <div className="toggle-row">
      <input id={id} type="checkbox" role="switch" className="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <label htmlFor={id}>
        <span>{label}</span>
        {description && <small className="hint">{description}</small>}
      </label>
    </div>
  );
}
