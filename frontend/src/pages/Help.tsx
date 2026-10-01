import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from '../api/client';
import type { ManualHit } from '../api/types';
import { Md } from '../components/Md';
import { Async, Badge } from '../components/ui';
import { useAsync } from '../lib/hooks';

/* The manual. Guides are written by hand; "What JARVIS can do" is generated from the live tool and action registries,
   so it always matches the running system. */
export function HelpPage() {
  const manual = useAsync(() => api.manual(), []);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<ManualHit[] | null>(null);
  const loc = useLocation();
  useEffect(() => {
    if (loc.hash && manual.data) document.getElementById(loc.hash.slice(1))?.scrollIntoView({ block: 'start' });
  }, [loc.hash, manual.data]);
  return (
    <div className="page help-page">
      <h1 className="sr-only">Help</h1>
      <form
        className="help-search"
        role="search"
        onSubmit={async (e) => {
          e.preventDefault();
          setHits(q.trim() ? await api.manualSearch(q.trim()) : null);
        }}
      >
        <label htmlFor="help-q" className="help-ask">
          How do I…
        </label>
        <input id="help-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="connect Slack, let tasks skip approval, run code…" />
        <button type="submit" className="btn btn-primary">
          Search
        </button>
      </form>
      {hits && (
        <section className="help-hits" aria-label="Search results">
          {hits.length === 0 ? (
            <p className="muted">Nothing matched. Try other words, or just ask JARVIS on the Command page.</p>
          ) : (
            hits.map((h, i) => (
              <a key={i} className="help-hit" href={`#${h.id}`}>
                <span className="small muted">{h.guide}</span>
                <Md text={h.text} />
              </a>
            ))
          )}
        </section>
      )}
      <Async state={manual}>
        {(m) => (
          <div className="help-layout">
            <nav className="help-toc" aria-label="Guides">
              {m.guides.map((g) => (
                <a key={g.id} href={`#${g.id}`}>
                  {g.title}
                </a>
              ))}
              <a href="#reference">What {m.assistant_name} can do</a>
              <a href="#tiers">What needs your approval</a>
            </nav>
            <div className="help-body">
              {m.guides.map((g) => (
                <article key={g.id} id={g.id} className="guide">
                  <h2>{g.title}</h2>
                  <Md text={g.markdown} skipTitle />
                </article>
              ))}
              <article id="reference" className="guide">
                <h2>What {m.assistant_name} can do</h2>
                <p className="muted small">Generated from the running system.</p>
                {m.reference.capabilities.map((c) => (
                  <section key={c.family}>
                    <h3>{c.label}</h3>
                    <ul className="plain cap-list">
                      {c.tools.map((t) => (
                        <li key={t.name}>
                          <span>{t.what}</span> <span className="muted small">{t.risk}</span>
                          {t.example && <span className="cap-example">“{t.example}”</span>}
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </article>
              <article id="tiers" className="guide">
                <h2>What needs your approval</h2>
                <table className="md-table">
                  <thead>
                    <tr>
                      <th scope="col">Change</th>
                      <th scope="col">Can a rule approve it?</th>
                    </tr>
                  </thead>
                  <tbody>
                    {m.reference.actions.map((a) => (
                      <tr key={a.action_type}>
                        <td>{a.label}</td>
                        <td>{a.automatable ? <Badge tone="ok">yes, if you create one</Badge> : <Badge tone="warn">no, always asks</Badge>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </article>
            </div>
          </div>
        )}
      </Async>
    </div>
  );
}
