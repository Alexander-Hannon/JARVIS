import { useRef, useState } from 'react';
import { api, errorMessage } from '../api/client';
import type { EvidencePreview, JDocument, Memory, MemoryStatus, SearchHit } from '../api/types';
import { Async, Badge, Card, Empty, ErrorBox, Loading, StateBadge, Tabs } from '../components/ui';
import { fmtDateTime } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { EvidencePreviewView } from './Console';

const STATUSES: (MemoryStatus | '')[] = ['', 'active', 'superseded', 'conflict', 'forgotten'];

function MemoryRow({ m, onChanged }: { m: Memory; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.content);
  const [confirmForget, setConfirmForget] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="memory card" data-testid="memory-row">
      <div className="row gap wrap">
        <StateBadge state={m.status} />
        <Badge tone="neutral">{m.type}</Badge>
        {m.pinned && <Badge tone="accent">pinned</Badge>}
        {m.confirmed_by_user && <Badge tone="ok">confirmed</Badge>}
        {m.entity && <span className="muted small">{m.entity}</span>}
        <span className="muted small">v{m.version}</span>
      </div>
      {editing ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              await api.correctMemory(m.id, draft.trim());
              setEditing(false);
            });
          }}
        >
          <label className="field">
            <span>Correction (creates a new version; the old one is kept as superseded)</span>
            <textarea rows={3} value={draft} onChange={(e) => setDraft(e.target.value)} />
          </label>
          <div className="row gap">
            <button className="btn btn-primary btn-small" type="submit" disabled={busy || !draft.trim() || draft.trim() === m.content}>
              Save correction
            </button>
            <button className="btn btn-small" type="button" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <p className="memory-content">{m.content}</p>
      )}
      <p className="muted small">
        {m.source_kind ? `Source: ${m.source_kind}${m.source_ref ? ` (${m.source_ref})` : ''} · ` : ''}
        {m.observed_at ? `observed ${fmtDateTime(m.observed_at)}` : ''}
        {m.review_at ? ` · review ${fmtDateTime(m.review_at)}` : ''}
        {m.superseded_by ? ` · superseded by ${m.superseded_by}` : ''}
        {m.conflict_with ? ` · conflicts with ${m.conflict_with}` : ''}
      </p>
      {m.status !== 'forgotten' && !editing && (
        <div className="row gap wrap">
          <button className="btn btn-small" type="button" disabled={busy} onClick={() => setEditing(true)}>
            Correct
          </button>
          <button className="btn btn-small" type="button" disabled={busy} onClick={() => void act(() => api.pinMemory(m.id, !m.pinned))}>
            {m.pinned ? 'Unpin' : 'Pin'}
          </button>
          {confirmForget ? (
            <span className="confirm">
              <span className="small">Forget this memory? It stops being used immediately. Existing backups may still contain it until they are rotated.</span>
              <button
                className="btn btn-small btn-danger"
                type="button"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const r = await api.forgetMemory(m.id);
                    setNote(r.note);
                    setConfirmForget(false);
                  })
                }
              >
                Forget
              </button>
              <button className="btn btn-small" type="button" onClick={() => setConfirmForget(false)}>
                Keep
              </button>
            </span>
          ) : (
            <button className="btn btn-small btn-danger-ghost" type="button" onClick={() => setConfirmForget(true)}>
              Forget…
            </button>
          )}
        </div>
      )}
      {note && <p className="muted small">{note}</p>}
      <ErrorBox error={err} />
    </li>
  );
}

function MemoryTab() {
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<MemoryStatus | ''>('active');
  const memories = useAsync(() => api.memories({ q: query, status }), [query, status]);
  const [adding, setAdding] = useState(false);
  const [content, setContent] = useState('');
  const [type, setType] = useState('fact');
  const [pinned, setPinned] = useState(false);
  const [addErr, setAddErr] = useState<unknown>(null);

  return (
    <>
      <div className="toolbar">
        <form
          className="inline grow"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            setQuery(q.trim());
          }}
        >
          <input aria-label="Search memories" placeholder="Search memories…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="btn" type="submit">
            Search
          </button>
        </form>
        <label className="inline small">
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value as MemoryStatus | '')}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || 'all'}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn-primary" type="button" onClick={() => setAdding((a) => !a)}>
          {adding ? 'Close' : 'Add memory'}
        </button>
        <a className="btn" href="/api/memories/export" download>
          Export (Markdown)
        </a>
      </div>
      {adding && (
        <Card title="Add a memory">
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setAddErr(null);
              try {
                await api.addMemory({ content: content.trim(), type, pinned });
                setContent('');
                setAdding(false);
                void memories.reload();
              } catch (x) {
                setAddErr(x);
              }
            }}
          >
            <label className="field">
              <span>Content</span>
              <textarea rows={3} value={content} onChange={(e) => setContent(e.target.value)} required />
            </label>
            <div className="row gap wrap">
              <label className="inline small">
                Type
                <select value={type} onChange={(e) => setType(e.target.value)}>
                  {['fact', 'preference', 'decision', 'commitment', 'person', 'context'].map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </label>
              <label className="inline small">
                <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} /> Pin
              </label>
              <button className="btn btn-primary" type="submit" disabled={!content.trim()}>
                Save
              </button>
            </div>
            <ErrorBox error={addErr} />
          </form>
        </Card>
      )}
      <Async state={memories} empty={query ? 'No memories match.' : 'No memories with this status.'}>
        {(list) => (
          <ul className="stack plain">
            {list.map((m) => (
              <MemoryRow key={`${m.id}-${m.version}`} m={m} onChanged={() => void memories.reload()} />
            ))}
          </ul>
        )}
      </Async>
    </>
  );
}

function segCount(d: JDocument): string {
  if (typeof d.segments === 'number') return String(d.segments);
  if (Array.isArray(d.segments)) return String(d.segments.length);
  return '—';
}

function DocDetail({ id }: { id: string }) {
  const doc = useAsync(() => api.document(id), [id]);
  const [question, setQuestion] = useState('');
  const [preview, setPreview] = useState<EvidencePreview | null>(null);
  const [err, setErr] = useState<unknown>(null);
  return (
    <div className="doc-detail">
      <Async state={doc}>
        {(d) => (
          <>
            <h4>Segments ({d.segments?.length ?? 0})</h4>
            {d.segments?.length ? (
              <ol className="segments">
                {d.segments.map((s) => (
                  <li key={s.id}>
                    <span className="muted small">
                      {s.locator} · {s.kind}
                    </span>
                    <p>{s.excerpt}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <Empty>No segments extracted.</Empty>
            )}
          </>
        )}
      </Async>
      <form
        className="inline"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          try {
            setPreview(await api.evidencePreview([id], question));
          } catch (x) {
            setErr(x);
          }
        }}
      >
        <input aria-label="Question about this document" placeholder="Question you plan to ask about this document" value={question} onChange={(e) => setQuestion(e.target.value)} />
        <button className="btn" type="submit" disabled={!question.trim()}>
          Preview what leaves the machine
        </button>
      </form>
      <ErrorBox error={err} />
      {preview && <EvidencePreviewView preview={preview} />}
    </div>
  );
}

function DocumentsTab() {
  const docs = useAsync(() => api.documents(), []);
  const [open, setOpen] = useState<string | null>(null);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const processing = docs.data?.some((d) => !['ready', 'done', 'processed', 'failed', 'error'].includes(d.status));
  useInterval(() => void docs.reload(), processing ? 3000 : null);

  return (
    <>
      <div className="toolbar">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={async (e) => {
            const files = Array.from(e.target.files ?? []);
            setUploadErr(null);
            for (const f of files) {
              setUploading((n) => n + 1);
              const form = new FormData();
              form.append('file', f, f.name);
              try {
                await api.uploadDocument(form);
              } catch (x) {
                setUploadErr(`${f.name}: ${errorMessage(x)}`);
              } finally {
                setUploading((n) => n - 1);
              }
            }
            if (fileRef.current) fileRef.current.value = '';
            void docs.reload();
          }}
        />
        <button className="btn btn-primary" type="button" onClick={() => fileRef.current?.click()} disabled={uploading > 0}>
          {uploading ? `Uploading ${uploading}…` : 'Upload documents'}
        </button>
        <span className="hint">Files are processed locally. Only excerpts you ask about are sent for reasoning.</span>
      </div>
      {uploadErr && <p className="bad-text">{uploadErr}</p>}
      <Async state={docs} empty="No documents uploaded yet.">
        {(list) => (
          <ul className="stack plain">
            {list.map((d) => {
              const v = d.processing?.visual;
              return (
                <li key={d.id} className="card doc">
                  <div className="row gap wrap">
                    <strong className="grow">{d.title}</strong>
                    <StateBadge state={d.status} />
                    <span className="muted small">
                      {d.mime} · v{d.version} · {segCount(d)} segments · {fmtDateTime(d.created_at)}
                    </span>
                  </div>
                  {d.processing && (
                    <div className="row gap wrap small">
                      {Object.entries(d.processing)
                        .filter(([k]) => k !== 'visual')
                        .map(([k, val]) => (
                          <span key={k} className="muted">
                            {k}: {typeof val === 'object' && val !== null ? ((val as { status?: string }).status ?? JSON.stringify(val)) : String(val)}
                          </span>
                        ))}
                      {v && (
                        <span>
                          Visual: {v.images ?? 0} images · {v.rendered_pages ?? 0} rendered pages
                          {v.needs_vision ? <Badge tone="warn">needs vision (sent to Anthropic if asked)</Badge> : null}
                        </span>
                      )}
                    </div>
                  )}
                  <div className="row gap">
                    <button className="btn btn-small" type="button" onClick={() => setOpen(open === d.id ? null : d.id)} aria-expanded={open === d.id}>
                      {open === d.id ? 'Hide' : 'Segments & evidence'}
                    </button>
                    <button
                      className="btn btn-small btn-danger-ghost"
                      type="button"
                      onClick={async () => {
                        if (!window.confirm(`Delete "${d.title}" from JARVIS?`)) return;
                        try {
                          await api.deleteDocument(d.id);
                          void docs.reload();
                        } catch (x) {
                          setUploadErr(errorMessage(x));
                        }
                      }}
                    >
                      Delete
                    </button>
                  </div>
                  {open === d.id && <DocDetail id={d.id} />}
                </li>
              );
            })}
          </ul>
        )}
      </Async>
    </>
  );
}

function SearchTab() {
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  return (
    <>
      <form
        className="inline"
        role="search"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            setHits(await api.search(q.trim()));
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <input aria-label="Search sources" placeholder="Search documents and notes…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn btn-primary" type="submit" disabled={!q.trim() || busy}>
          Search
        </button>
      </form>
      <ErrorBox error={err} />
      {busy && <Loading label="Searching…" />}
      {hits && !busy && (hits.length === 0 ? <Empty>No results.</Empty> : (
        <ol className="stack plain">
          {hits.map((h, i) => (
            <li key={`${h.segment_id ?? h.document_id}-${i}`} className="card">
              <div className="row gap wrap">
                <strong>{h.title}</strong>
                <span className="muted small">{h.locator}</span>
                <Badge tone="neutral">{h.kind}</Badge>
                <span className="muted small">score {h.score.toFixed(2)}</span>
              </div>
              <blockquote>{h.excerpt}</blockquote>
            </li>
          ))}
        </ol>
      ))}
    </>
  );
}

export function MemoryPage() {
  const [tab, setTab] = useState<'memory' | 'documents' | 'search'>('memory');
  return (
    <div className="page">
      <header className="page-head">
        <h1>Memory &amp; knowledge</h1>
      </header>
      <Tabs
        label="Memory views"
        value={tab}
        onChange={setTab}
        tabs={[
          { key: 'memory', label: 'Memories' },
          { key: 'documents', label: 'Documents' },
          { key: 'search', label: 'Search sources' },
        ]}
      />
      {tab === 'memory' && <MemoryTab />}
      {tab === 'documents' && <DocumentsTab />}
      {tab === 'search' && <SearchTab />}
    </div>
  );
}
