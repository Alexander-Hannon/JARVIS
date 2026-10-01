import { useState } from 'react';
import { api } from '../api/client';
import type { McpMode, McpServer } from '../api/types';
import { useAsync } from '../lib/hooks';
import { Async, Badge, Card, ErrorBox } from './ui';

/* MCP servers: every tool is off until you review it. A changed tool list disables the server until you look again. */

const STATUS: Record<McpServer['status'], { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  active: { label: 'active', tone: 'ok' },
  pending_review: { label: 'needs review', tone: 'warn' },
  changed: { label: 'tools changed: review', tone: 'bad' },
  error: { label: 'unreachable', tone: 'bad' },
  disabled: { label: 'disabled', tone: 'neutral' },
};

function Review({ s, onDone }: { s: McpServer; onDone: () => void }) {
  const [modes, setModes] = useState<Record<string, { mode: McpMode; tier: 1 | 2 }>>(() =>
    Object.fromEntries(s.tools.map((t) => [t.name, { mode: t.mode, tier: t.tier }])),
  );
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="mcp-review">
      <table className="pick-table">
        <thead>
          <tr>
            <th scope="col">Tool</th>
            <th scope="col">Use</th>
            <th scope="col">Automatable</th>
          </tr>
        </thead>
        <tbody>
          {s.tools.map((t) => {
            const m = modes[t.name] ?? { mode: 'off', tier: 2 };
            return (
              <tr key={t.name}>
                <th scope="row">
                  <code>{t.name}</code> {t.annotations.read_only && <Badge tone="info">read-only</Badge>}{' '}
                  {t.annotations.destructive && <Badge tone="bad">destructive</Badge>}
                  <div className="muted small">{t.description}</div>
                </th>
                <td>
                  <select
                    aria-label={`Use of ${t.name}`}
                    value={m.mode}
                    onChange={(e) => setModes({ ...modes, [t.name]: { mode: e.target.value as McpMode, tier: 2 } })}
                  >
                    <option value="off">Off</option>
                    {t.annotations.read_only && <option value="read">Read freely</option>}
                    <option value="propose">Ask me first</option>
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Allow automation rules for ${t.name}`}
                    disabled={m.mode !== 'propose' || t.annotations.destructive}
                    checked={m.mode === 'propose' && m.tier === 1}
                    onChange={(e) => setModes({ ...modes, [t.name]: { mode: 'propose', tier: e.target.checked ? 1 : 2 } })}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted small">
        "Read freely" only appears for tools the server marks read-only. "Automatable" lets a rule in Actions → Policies approve that
        tool for you. Destructive tools always ask.
      </p>
      <button
        type="button"
        className="btn btn-primary btn-small"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setErr(null);
          try {
            await api.reviewMcp(s.id, modes);
            onDone();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        Approve this tool list
      </button>
      <ErrorBox error={err} />
    </div>
  );
}

function ServerRow({ s, onChanged }: { s: McpServer; onChanged: () => void }) {
  const [open, setOpen] = useState(s.status === 'pending_review' || s.status === 'changed');
  const [err, setErr] = useState<unknown>(null);
  const st = STATUS[s.status];
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <li className="connector" data-testid={`mcp-${s.name}`}>
      <button type="button" className="connector-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="connector-name">{s.name}</span>
        <Badge tone={st.tone}>{s.simulated ? `${st.label} · simulated` : st.label}</Badge>
        <span className="muted small connector-sum">
          {s.transport === 'http' ? s.url : [s.command, ...s.args].join(' ')} · {s.tools.filter((t) => t.mode !== 'off').length}/
          {s.tools.length} tools on
        </span>
      </button>
      {open && (
        <div className="connector-body">
          {s.status === 'changed' && (
            <p className="warn-text">The server's tools changed since you reviewed them. Nothing runs until you approve the new list.</p>
          )}
          {s.last_error && <p className="bad-text small">{s.last_error}</p>}
          <Review s={s} onDone={onChanged} />
          <div className="row gap wrap">
            <button type="button" className="btn btn-small" onClick={() => void act(() => api.discoverMcp(s.id))}>
              Re-check tools
            </button>
            <button type="button" className="btn btn-small" onClick={() => void act(() => api.enableMcp(s.id, s.status === 'disabled'))}>
              {s.status === 'disabled' ? 'Enable' : 'Disable'}
            </button>
            <button
              type="button"
              className="btn btn-small btn-danger-ghost"
              onClick={() => window.confirm(`Remove MCP server ${s.name}?`) && void act(() => api.removeMcp(s.id))}
            >
              Remove
            </button>
          </div>
          <ErrorBox error={err} />
        </div>
      )}
    </li>
  );
}

function AddServer({ onDone }: { onDone: () => void }) {
  const [transport, setTransport] = useState<'http' | 'stdio'>('http');
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [bearer, setBearer] = useState('');
  const [env, setEnv] = useState('');
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="connector-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        try {
          const envObj = Object.fromEntries(
            env
              .split('\n')
              .map((l) => l.trim())
              .filter((l) => l.includes('='))
              .map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]),
          );
          await api.addMcpServer({
            name: name.trim(),
            transport,
            ...(transport === 'http'
              ? { url: url.trim(), bearer: bearer.trim() || undefined }
              : { command: command.trim(), args: args.trim() ? args.trim().split(/\s+/) : [], env: Object.keys(envObj).length ? envObj : undefined }),
          });
          setName('');
          setUrl('');
          setCommand('');
          setArgs('');
          setBearer('');
          setEnv('');
          onDone();
        } catch (x) {
          setErr(x);
        } finally {
          setBusy(false);
        }
      }}
    >
      <h4>Add an MCP server</h4>
      <label className="field">
        <span>Name (used in tool names)</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="github" />
      </label>
      <label className="field">
        <span>Connection</span>
        <select value={transport} onChange={(e) => setTransport(e.target.value as 'http' | 'stdio')}>
          <option value="http">Remote / HTTP (recommended)</option>
          <option value="stdio">Local command (runs on the JARVIS host)</option>
        </select>
      </label>
      {transport === 'http' ? (
        <>
          <label className="field">
            <span>URL</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp.example.com/mcp" />
          </label>
          <label className="field">
            <span>Bearer token (optional)</span>
            <input type="password" autoComplete="off" value={bearer} onChange={(e) => setBearer(e.target.value)} />
          </label>
        </>
      ) : (
        <>
          <p className="warn-text small">
            A local server is a program running with JARVIS's permissions on this machine. Only add commands you trust and typed
            yourself.
          </p>
          <label className="field">
            <span>Executable</span>
            <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" />
          </label>
          <label className="field">
            <span>Arguments</span>
            <input value={args} onChange={(e) => setArgs(e.target.value)} placeholder="-y @modelcontextprotocol/server-everything" />
          </label>
          <label className="field">
            <span>Environment (KEY=value per line, stored encrypted)</span>
            <textarea rows={2} value={env} onChange={(e) => setEnv(e.target.value)} />
          </label>
        </>
      )}
      <button type="submit" className="btn btn-primary" disabled={busy || !name.trim() || (transport === 'http' ? !url.trim() : !command.trim())}>
        {busy ? 'Connecting…' : 'Add and list tools'}
      </button>
      <ErrorBox error={err} />
    </form>
  );
}

export function McpServers() {
  const list = useAsync(() => api.mcpServers(), []);
  const reload = () => void list.reload();
  return (
    <Card title="MCP servers" id="connect-mcp">
      <p className="muted small">
        Plug any MCP server into JARVIS. Its tools stay off until you review them, and anything that changes data goes through your
        approvals. If a server's tools change later, JARVIS stops using it until you review again.
      </p>
      <Async state={list} empty="No MCP servers yet.">
        {(items) => (
          <ul className="plain connectors">
            {items.map((s) => (
              <ServerRow key={s.id} s={s} onChanged={reload} />
            ))}
          </ul>
        )}
      </Async>
      <AddServer onDone={reload} />
    </Card>
  );
}
