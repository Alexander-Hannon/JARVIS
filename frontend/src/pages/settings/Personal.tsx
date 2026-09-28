import { useState } from 'react';
import { api } from '../../api/client';
import type { PairingCode, VoiceStatus } from '../../api/types';
import { SpeechControls } from '../../components/SpeechControls';
import { Async, Badge, Card, ErrorBox, KV, StateBadge, Toggle } from '../../components/ui';
import { fmtDateTime, fmtRelative } from '../../lib/format';
import { useAsync } from '../../lib/hooks';
import { CANDIDATE_MIME_TYPES, micCapabilityProblem, pickMimeType } from '../../voice/recorder';

export function VoiceSection() {
  const status = useAsync(() => api.voiceStatus(), []);
  const settings = useAsync(() => api.settings(), []);
  const [warming, setWarming] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const mic = micCapabilityProblem();
  const mime = typeof MediaRecorder !== 'undefined' ? pickMimeType() || 'browser default' : 'n/a';

  const warm = async () => {
    setWarming(true);
    setErr(null);
    try {
      status.setData(await api.voiceWarmup());
    } catch (e) {
      setErr(e);
    } finally {
      setWarming(false);
    }
  };

  return (
    <div className="stack">
      <Card title="Speech-to-text (local)">
        <Async state={status}>
          {(s: VoiceStatus) => (
            <>
              <KV
                items={[
                  ['Engine', `${s.engine ?? '—'}${s.model ? ` · ${s.model}` : ''}${s.device ? ` · ${s.device}` : ''}`],
                  ['State', <StateBadge key="s" state={s.error ? 'error' : s.state} />],
                  ['Model downloaded', s.downloaded ? 'Yes' : `No${s.download_size_mb ? ` (~${s.download_size_mb} MB download on first warm-up)` : ''}`],
                  ['Load time', s.load_ms != null ? `${s.load_ms} ms` : 'not measured'],
                  ['Warm-up time', s.warmup_ms != null ? `${s.warmup_ms} ms` : 'not measured'],
                  ['Last transcription latency', s.last_latency_ms != null ? `${s.last_latency_ms} ms` : 'not measured yet'],
                  ['Runs locally', s.local_only ? <Badge key="l" tone="ok">local only — audio does not leave this machine</Badge> : <Badge key="l" tone="warn">cloud STT enabled — audio leaves this machine</Badge>],
                  ['Hardware', typeof s.hardware === 'object' && s.hardware ? Object.entries(s.hardware as Record<string, unknown>).map(([k, v]) => `${k}: ${String(v)}`).join(' · ') : String(s.hardware ?? '—')],
                ]}
              />
              {s.error && <p className="bad-text">{s.error}</p>}
              <button type="button" className="btn btn-primary" onClick={() => void warm()} disabled={warming}>
                {warming ? 'Warming up… (first run may download the model)' : s.downloaded ? 'Warm up model' : `Download & warm up${s.download_size_mb ? ` (~${s.download_size_mb} MB)` : ''}`}
              </button>
              <ErrorBox error={err} />
            </>
          )}
        </Async>
      </Card>
      <Card title="This device">
        <KV
          items={[
            ['Microphone', mic ? <span className="warn-text">{mic.message}</span> : 'Available (permission is requested when you first record)'],
            ['Recording format', mime],
            ['Formats checked', CANDIDATE_MIME_TYPES.join(', ')],
          ]}
        />
      </Card>
      <Card title="Speech output">
        <p className="hint">JARVIS reads short spoken summaries only. Voice choice is saved per device.</p>
        <SpeechControls full />
      </Card>
      <Card title="Audio retention">
        <Async state={settings}>
          {(st) => (
            <Toggle
              label="Keep raw audio after transcription"
              description="Off by default: audio is deleted once transcribed. Turn on only if you want recordings kept for review."
              checked={!!st.privacy?.retain_audio}
              onChange={async (v) => {
                try {
                  settings.setData(await api.patchSettings({ privacy: { retain_audio: v } }));
                } catch (e) {
                  setErr(e);
                }
              }}
            />
          )}
        </Async>
      </Card>
    </div>
  );
}

export function PrivacySection() {
  return (
    <Card title="Privacy & data flow">
      <ul className="dataflow">
        <li>
          <strong>Stays on this machine:</strong> your database, memories, documents, audio (deleted after transcription by default), and local speech-to-text.
        </li>
        <li>
          <strong>Leaves this machine:</strong> prompts, selected document excerpts and images sent to Anthropic for reasoning; calls to Todoist/Google/GitHub you
          connected. Use “What leaves the machine?” before asking about documents to see exactly which blocks are sent.
        </li>
        <li>
          <strong>Secrets:</strong> API keys and OAuth tokens are encrypted server-side, never shown in the browser again, and excluded from exports/backups by
          default. The offline cache stores only the app shell, never API responses.
        </li>
        <li>
          <strong>No telemetry</strong> or third-party tracking.
        </li>
        <li>
          <strong>Voice is not identity:</strong> the microphone does not verify who is speaking; your signed-in session does.
        </li>
      </ul>
    </Card>
  );
}

export function LocalOpsSection() {
  const roots = useAsync(() => api.localRoots(), []);
  const [path, setPath] = useState('');
  const [label, setLabel] = useState('');
  const [writable, setWritable] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  return (
    <Card title="Local workspace roots">
      <p className="hint">JARVIS can only list/read files inside these folders. Writes (notes, renames) always need your approval. Hidden and credential folders are always blocked.</p>
      <Async state={roots} empty="No workspace roots. JARVIS cannot access any local files.">
        {(list) => (
          <ul className="list">
            {list.map((r) => (
              <li key={r.id} className="list-row">
                <div className="grow">
                  <strong>{r.label}</strong> <code className="small">{r.path}</code> {r.writable ? <Badge tone="warn">writable (with approval)</Badge> : <Badge tone="neutral">read-only</Badge>}
                </div>
                {confirmId === r.id ? (
                  <span className="confirm">
                    <button
                      type="button"
                      className="btn btn-small btn-danger"
                      onClick={async () => {
                        try {
                          await api.removeLocalRoot(r.id);
                          void roots.reload();
                        } catch (e) {
                          setErr(e);
                        }
                        setConfirmId(null);
                      }}
                    >
                      Remove
                    </button>
                    <button type="button" className="btn btn-small" onClick={() => setConfirmId(null)}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => setConfirmId(r.id)}>
                    Remove…
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Async>
      <form
        className="grid-form"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          try {
            await api.addLocalRoot({ path: path.trim(), label: label.trim() || path.trim(), writable });
            setPath('');
            setLabel('');
            setWritable(false);
            void roots.reload();
          } catch (x) {
            setErr(x);
          }
        }}
      >
        <label className="field">
          <span>Folder path on the JARVIS host</span>
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/home/you/Documents/jarvis" />
        </label>
        <label className="field">
          <span>Label</span>
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <Toggle label="Allow approved writes" checked={writable} onChange={setWritable} />
        <button type="submit" className="btn btn-primary" disabled={!path.trim()}>
          Add root
        </button>
      </form>
      <ErrorBox error={err} />
    </Card>
  );
}

export function SecuritySection({ onLoggedOut }: { onLoggedOut: () => void }) {
  const [code, setCode] = useState<PairingCode | null>(null);
  const [err, setErr] = useState<unknown>(null);
  return (
    <Card title="Security & devices">
      <h4>Pair a phone</h4>
      <p className="hint">Creates a short-lived one-time code. On the phone, open JARVIS, choose “Pair with a code”, and enter it.</p>
      <button
        type="button"
        className="btn"
        onClick={async () => {
          setErr(null);
          try {
            setCode(await api.pairingCode());
          } catch (e) {
            setErr(e);
          }
        }}
      >
        Create pairing code
      </button>
      {code && (
        <div className="pair-code" role="status">
          <span className="mono big">{code.code}</span>
          <span className="muted small">
            expires {fmtRelative(code.expires_at)} ({fmtDateTime(code.expires_at)})
          </span>
        </div>
      )}
      <h4>Session</h4>
      <button
        type="button"
        className="btn btn-danger-ghost"
        onClick={async () => {
          try {
            await api.logout();
          } finally {
            onLoggedOut();
          }
        }}
      >
        Sign out
      </button>
      <ErrorBox error={err} />
    </Card>
  );
}
