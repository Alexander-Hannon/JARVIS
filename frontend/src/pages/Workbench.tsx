import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { SandboxRun } from '../api/types';
import { PageHead } from '../components/PageHead';
import { Async, Badge, Card, ErrorBox, Toggle } from '../components/ui';
import { useAsync } from '../lib/hooks';

/* The sandbox, made visible: its screen, every command it ran, its internet window, and your skills. */

function Screen({ shot }: { shot: string | null }) {
  return (
    <figure className="sbx-screen">
      {shot ? <img src={`/api/sandbox/shots/${shot}`} alt="Latest sandbox screen" /> : <div className="sbx-blank">No screen captured yet</div>}
      <figcaption className="small muted">Latest view of the sandbox. It refreshes while JARVIS works.</figcaption>
    </figure>
  );
}

function Transcript({ runs }: { runs: SandboxRun[] }) {
  if (!runs.length) return <p className="muted small">JARVIS hasn't used the sandbox yet.</p>;
  return (
    <ol className="plain sbx-log" aria-label="Sandbox activity">
      {runs.map((r) => (
        <li key={r.id} className={`sbx-${r.kind}`}>
          <div className="sbx-cmd">
            <span className="mono small muted">{new Date(r.created_at).toLocaleTimeString()}</span> <code>{r.kind === 'exec' ? `$ ${r.detail}` : `${r.kind}: ${r.detail}`}</code>
            {r.exit_code !== null && r.exit_code !== 0 && <Badge tone="bad">exit {r.exit_code}</Badge>}
          </div>
          {r.output && <pre className="sbx-out">{r.output}</pre>}
        </li>
      ))}
    </ol>
  );
}

function Skills() {
  const list = useAsync(() => api.skills(), []);
  const [folder, setFolder] = useState('');
  const [err, setErr] = useState<unknown>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      void list.reload();
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <Card title="Skills" id="skills">
      <p className="muted small">Saved how-tos JARVIS loads when relevant. Ask JARVIS to draft one ("remember how I like my weekly report"), or install a folder containing a SKILL.md.</p>
      <Async state={list} empty="No skills yet.">
        {(items) => (
          <ul className="plain connectors">
            {items.map((s) => (
              <li key={s.name} className="connector">
                <div className="connector-body">
                  <div className="row gap wrap">
                    <strong>{s.title}</strong> {!s.enabled && <Badge tone="neutral">off</Badge>}
                    <span className="muted small">{s.description}</span>
                  </div>
                  <div className="row gap">
                    <button type="button" className="btn btn-small" onClick={() => void act(() => api.skillEnabled(s.name, !s.enabled))}>
                      {s.enabled ? 'Turn off' : 'Turn on'}
                    </button>
                    <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => window.confirm(`Remove skill ${s.title}?`) && void act(() => api.removeSkill(s.name))}>
                      Remove
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Async>
      <form
        className="row gap wrap"
        onSubmit={(e) => {
          e.preventDefault();
          void act(async () => {
            await api.installSkill(folder.trim());
            setFolder('');
          });
        }}
      >
        <input aria-label="Skill folder path" value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="/home/you/skills/weekly-report" />
        <button type="submit" className="btn btn-small" disabled={!folder.trim()}>
          Install from folder
        </button>
      </form>
      <ErrorBox error={err} />
    </Card>
  );
}

export function WorkbenchPage() {
  const st = useAsync(() => api.sandbox(), []);
  const hist = useAsync(() => api.sandboxHistory(60), []);
  const [err, setErr] = useState<unknown>(null);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setInterval(() => {
      void st.reload();
      void hist.reload();
    }, 4000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const op = async (o: 'start' | 'stop' | 'reset' | 'build' | 'network/close', confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setErr(null);
    try {
      await api.sandboxDo(o);
      if (o === 'build') setNote('Building the sandbox image in the background. This takes a few minutes the first time.');
      void st.reload();
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <div className="page workbench-page">
      <PageHead title="Workbench" guide="workbench">
        JARVIS's own computer: a disposable sandbox where it runs code, browses and works a desktop. It can't reach your files or accounts.
      </PageHead>
      <Async state={st}>
        {(s) => (
          <div className="wb-grid">
            <section className="wb-main">
              {s.available ? (
                <Screen shot={s.latest_shot} />
              ) : (
                <div className="sbx-setup">
                  <h2>Set up the sandbox</h2>
                  <p>{s.detail}</p>
                  <ol>
                    <li>Open Docker Desktop on Windows → Settings → Resources → WSL integration.</li>
                    <li>Turn it on for your Ubuntu distro, then Apply &amp; restart.</li>
                    <li>Restart JARVIS, come back here and choose Build sandbox (a few minutes, once).</li>
                  </ol>
                </div>
              )}
              <div className="row gap wrap wb-controls">
                <Badge tone={!s.available ? 'bad' : s.running ? 'ok' : 'neutral'}>
                  {!s.available ? 'unavailable' : s.running ? (s.simulated ? 'running · simulated' : 'running') : 'stopped'}
                </Badge>
                {s.available && !s.image && s.runtime === 'docker' && (
                  <button type="button" className="btn btn-small btn-primary" onClick={() => void op('build')}>
                    Build sandbox
                  </button>
                )}
                {s.available && (s.running ? (
                  <button type="button" className="btn btn-small" onClick={() => void op('stop')}>
                    Stop
                  </button>
                ) : (
                  <button type="button" className="btn btn-small" onClick={() => void op('start')}>
                    Start
                  </button>
                ))}
                {s.available && (
                  <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => void op('reset', 'Reset the sandbox? Its workspace files are erased.')}>
                    Reset
                  </button>
                )}
              </div>
              {note && <p className="small muted">{note}</p>}
              <div className="wb-net">
                {s.network.active ? (
                  <p>
                    Internet open to <strong>{s.network.domains.join(', ')}</strong> until {new Date(s.network.until!).toLocaleTimeString()}.{' '}
                    <button type="button" className="btn btn-small" onClick={() => void op('network/close')}>
                      Close now
                    </button>
                  </p>
                ) : (
                  <p className="muted small">No internet. JARVIS asks before opening access to specific sites.</p>
                )}
              </div>
              <Toggle
                label="Let JARVIS use the sandbox desktop (mouse and keyboard)"
                checked={s.computer_use ?? true}
                onChange={(v) => void api.sandboxSettings(v).then(() => st.reload(), setErr)}
              />
            </section>
            <section className="wb-log card">
              <h2>Activity</h2>
              <Async state={hist}>{(runs) => <Transcript runs={runs} />}</Async>
            </section>
          </div>
        )}
      </Async>
      <ErrorBox error={err} />
      <Skills />
    </div>
  );
}
