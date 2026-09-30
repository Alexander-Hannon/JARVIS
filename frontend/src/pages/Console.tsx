import { MessageText } from '../components/MessageText';
import { Visuals } from '../components/Visuals';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../api/client';
import type { CommandResponse, EvidencePreview, JDocument, RunEvent } from '../api/types';
import { ProposalCard } from '../components/ProposalCard';
import { SpeechControls } from '../components/SpeechControls';
import { VoicePanel } from '../components/VoicePanel';
import { Badge, ErrorBox, StateBadge } from '../components/ui';
import { fmtBytes, fmtTime } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { useConversation, useSpeech } from '../state/app';

const ACTIVE_RUN = new Set(['running', 'queued', 'pending', 'in_progress', 'started', 'executing']);

function Events({ events }: { events: RunEvent[] }) {
  if (!events.length) return null;
  return (
    <details className="events" open={events.length <= 4}>
      <summary>Activity ({events.length})</summary>
      <ol>
        {events.map((e) => (
          <li key={e.id}>
            <span className="muted small">{fmtTime(e.created_at)}</span> {e.message}
          </li>
        ))}
      </ol>
    </details>
  );
}

function ResponseView({ resp }: { resp: CommandResponse }) {
  const conv = useConversation();
  const speech = useSpeech();
  const [cancelState, setCancelState] = useState<string | null>(null);
  const active = ACTIVE_RUN.has(resp.status) && cancelState === null;

  // Poll run events while the run is still active.
  useEffect(() => {
    if (!active || !resp.run_id) return;
    let stop = false;
    const started = Date.now();
    const tick = async () => {
      try {
        const events = await api.runEvents(resp.run_id);
        if (!stop) conv.updateResponse(resp.run_id, (r) => ({ ...r, events }));
      } catch {
        /* transient */
      }
      if (!stop && Date.now() - started < 120000) timer = window.setTimeout(tick, 1500);
    };
    let timer = window.setTimeout(tick, 1500);
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [active, resp.run_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const correction = useCallback(
    async (text: string) => {
      await conv.send({ text, channel: 'typed', captured_at: new Date().toISOString(), speech_playing: false });
    },
    [conv],
  );

  return (
    <div className="bubble bubble-assistant" data-testid="assistant-response">
      <div className="bubble-meta">
        <span>JARVIS</span>
        <StateBadge state={cancelState ?? resp.status} />
        {resp.intent && <Badge tone="neutral">{resp.intent}</Badge>}
        {resp.specialists?.map((s) => (
          <Badge key={s} tone="info">
            {s}
          </Badge>
        ))}
      </div>
      <Events events={resp.events ?? []} />
      <MessageText text={resp.message?.text} />
      <Visuals items={resp.visuals} />
      {resp.questions?.length > 0 && (
        <ul className="questions">
          {resp.questions.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      )}
      {resp.citations?.length > 0 && (
        <div className="citations">
          <h4>Sources</h4>
          <ul>
            {resp.citations.map((c, i) => (
              <li key={i}>
                {c.url ? (
                  <a href={c.url} target="_blank" rel="noreferrer noopener">
                    <strong>{c.title || c.url}</strong>
                  </a>
                ) : (
                  <strong>{c.title}</strong>
                )}{' '}
                <span className="muted">({c.locator})</span>
                {c.excerpt && <blockquote>{c.excerpt}</blockquote>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="row gap">
        {resp.message?.spoken_text && (
          <button type="button" className="btn btn-small btn-ghost" onClick={() => speech.speak(resp.message.spoken_text)} disabled={speech.prefs.muted} title={speech.prefs.muted ? 'Voice is muted' : undefined}>
            ▶ Replay
          </button>
        )}
        {active && (
          <button
            type="button"
            className="btn btn-small btn-danger-ghost"
            onClick={async () => {
              try {
                const r = await api.cancelRun(resp.run_id);
                setCancelState(r.status || 'cancelled');
              } catch (e) {
                setCancelState(`cancel failed: ${errorMessage(e)}`);
              }
            }}
          >
            Cancel this run
          </button>
        )}
      </div>
      {resp.proposal && (
        <ProposalCard proposal={resp.proposal} onCorrection={correction} onChanged={(np) => conv.updateResponse(resp.run_id, (r) => ({ ...r, proposal: np }))} />
      )}
    </div>
  );
}

interface Attachment {
  key: string;
  name: string;
  size: number;
  status: 'uploading' | 'ready' | 'error';
  doc?: JDocument;
  error?: string;
}

export function ConsolePage() {
  const conv = useConversation();
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [preview, setPreview] = useState<EvidencePreview | null>(null);
  const [previewErr, setPreviewErr] = useState<unknown>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const settings = useAsync(() => api.settings(), []);
  const retainAudio = !!settings.data?.privacy?.retain_audio;

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: 'end', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }, [conv.thread.length]);

  const focusTyped = useCallback(() => {
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, []);

  const addFiles = async (files: FileList | null) => {
    if (!files) return;
    for (const f of Array.from(files)) {
      const key = `${f.name}-${Date.now()}-${Math.random()}`;
      setAttachments((a) => [...a, { key, name: f.name, size: f.size, status: 'uploading' }]);
      const form = new FormData();
      form.append('file', f, f.name);
      try {
        const doc = await api.uploadDocument(form);
        setAttachments((a) => a.map((x) => (x.key === key ? { ...x, status: 'ready', doc } : x)));
      } catch (e) {
        setAttachments((a) => a.map((x) => (x.key === key ? { ...x, status: 'error', error: errorMessage(e) } : x)));
      }
    }
    if (fileRef.current) fileRef.current.value = '';
    setPreview(null);
  };

  const readyIds = attachments.filter((a) => a.status === 'ready' && a.doc).map((a) => a.doc!.id);
  const uploading = attachments.some((a) => a.status === 'uploading');

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || uploading) return;
    const captured_at = new Date().toISOString();
    setText('');
    const ids = readyIds;
    setAttachments([]);
    setPreview(null);
    await conv.send({ text: t, channel: 'typed', captured_at, speech_playing: false, attachments: ids.length ? ids : undefined });
    inputRef.current?.focus();
  };

  const sendReason = !text.trim() ? 'Type a command' : uploading ? 'Waiting for attachments to upload' : conv.sending ? 'Sending…' : null;

  return (
    <div className="page console">
      <header className="page-head">
        <h1>Command</h1>
        <div className="row gap wrap console-head-actions">
          <SpeechControls />
          <button type="button" className="btn btn-small btn-ghost" onClick={() => void conv.newConversation()} disabled={conv.sending}>
            New conversation
          </button>
        </div>
      </header>

      <div className="thread" aria-live="polite" data-testid="thread">
        {conv.loadError && <ErrorBox error={conv.loadError} />}
        {conv.thread.length === 0 && <p className="empty">Say or type what you need: “Add review lab report tomorrow at 3pm”, “What’s due this week?”</p>}
        {conv.thread.map((e) => {
          if (e.kind === 'user')
            return (
              <div key={e.id} className="bubble bubble-user">
                <div className="bubble-meta">
                  <span>You</span>
                  <Badge tone={e.channel === 'voice' ? 'accent' : 'neutral'}>{e.channel}</Badge>
                  <span className="muted small">{fmtTime(e.at)}</span>
                </div>
                <p className="bubble-text">{e.text}</p>
              </div>
            );
          if (e.kind === 'history')
            return (
              <div key={e.id} className={`bubble ${e.message.role === 'user' ? 'bubble-user' : 'bubble-assistant'}`}>
                <div className="bubble-meta">
                  <span>{e.message.role === 'user' ? 'You' : 'JARVIS'}</span>
                  <span className="muted small">{fmtTime(e.message.created_at)}</span>
                </div>
                <MessageText text={e.message.text} />
              </div>
            );
          if (e.kind === 'error')
            return (
              <div key={e.id} className="bubble bubble-error" role="alert">
                <strong>Command failed.</strong> {e.text}
              </div>
            );
          return <ResponseView key={e.id} resp={e.resp} />;
        })}
        {conv.sending && (
          <div className="thinking" role="status" aria-live="polite">
            <span className="thinking-core" aria-hidden="true" />
            <div>
              <strong>JARVIS is working…</strong>
              {conv.liveEvents.slice(-6).map((e, i) => (
                <div key={i} className="thinking-line">
                  › {e}
                </div>
              ))}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="composer-wrap">
        <VoicePanel focusTyped={focusTyped} retainAudio={retainAudio} />

        <form className="composer" onSubmit={submit}>
          {attachments.length > 0 && (
            <ul className="chips" aria-label="Attachments">
              {attachments.map((a) => (
                <li key={a.key} className={`chip chip-${a.status}`}>
                  {a.name} <span className="muted">{fmtBytes(a.size)}</span> · {a.status === 'error' ? <span className="bad-text">{a.error}</span> : a.status}
                  <button type="button" className="chip-x" aria-label={`Remove ${a.name}`} onClick={() => setAttachments((l) => l.filter((x) => x.key !== a.key))}>
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
          <label htmlFor="cmd-input" className="sr-only">
            Type a command
          </label>
          <textarea
            id="cmd-input"
            ref={inputRef}
            data-testid="cmd-input"
            rows={2}
            placeholder="Type a command…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void submit();
              }
            }}
          />
          <div className="composer-actions">
            <input ref={fileRef} type="file" multiple hidden onChange={(e) => void addFiles(e.target.files)} data-testid="file-input" />
            <button type="button" className="btn" onClick={() => fileRef.current?.click()}>
              Attach
            </button>
            {readyIds.length > 0 && (
              <button
                type="button"
                className="btn btn-ghost"
                onClick={async () => {
                  setPreviewErr(null);
                  try {
                    setPreview(await api.evidencePreview(readyIds, text));
                  } catch (x) {
                    setPreviewErr(x);
                  }
                }}
              >
                What leaves the machine?
              </button>
            )}
            <span className="spacer" />
            <button type="submit" className="btn btn-primary" disabled={!!sendReason} title={sendReason ?? undefined} data-testid="send-btn">
              Send
            </button>
          </div>
          <ErrorBox error={previewErr} />
          {preview && <EvidencePreviewView preview={preview} />}
        </form>
      </div>
    </div>
  );
}

export function EvidencePreviewView({ preview }: { preview: EvidencePreview }) {
  return (
    <div className="notice notice-warn evidence">
      <strong>{preview.leaves_machine ? 'These blocks would be sent to Anthropic (leaves this machine):' : 'Evidence stays local:'}</strong>
      <ul>
        {preview.blocks.map((b, i) => (
          <li key={i}>
            {b.type === 'image' ? 'Image' : 'Text'} · {b.locator} · {b.type === 'image' ? `${fmtBytes(b.bytes)} ${b.media_type ?? ''}` : `${b.chars ?? '?'} chars`}
          </li>
        ))}
      </ul>
      <span>Estimated input: ~{preview.estimated_input_tokens.toLocaleString()} tokens</span>
    </div>
  );
}
