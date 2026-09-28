import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, errorMessage, uploadWithProgress } from '../api/client';
import type { AudioErrorCode, Capture } from '../api/types';
import { fmtElapsed } from '../lib/format';
import { useConversation, useSpeech } from '../state/app';
import { AudioRecorder, extensionFor, MAX_RECORDING_S, MicError, micCapabilityProblem, type Recording } from '../voice/recorder';
import { tts } from '../voice/tts';
import { SilenceDetector } from '../voice/vad';

type Phase = 'idle' | 'starting' | 'recording' | 'uploading' | 'transcribing' | 'review' | 'sending';

interface Draft {
  capture: Capture;
  text: string;
  capturedAt: string;
  tz: string;
  speechPlaying: boolean;
  handsFree: boolean;
}

interface VoiceErr {
  kind: 'mic' | 'audio' | 'upload';
  code: string;
  message: string;
}

const AUDIO_ERROR_TEXT: Record<AudioErrorCode, string> = {
  stt_unavailable: 'The speech-to-text engine is not available on the server. Check Settings → Voice (warm-up / download). Your command was not processed — type it instead.',
  decode_failed: 'The server could not decode this recording. Try again, or type your command.',
  too_large: 'That recording is too large to upload. Keep voice commands shorter.',
  too_long: `That recording is longer than the server allows (max ${MAX_RECORDING_S / 60} minutes).`,
  empty_audio: 'The recording was empty — no audio reached the server. Check your microphone input.',
  unsupported_format: 'This browser recorded an audio format the server does not accept.',
  no_speech: 'No speech was detected in that recording. Try again closer to the microphone.',
};

const LOW_CONFIDENCE = 0.6;
const HOLD_MS = 350;

export function VoicePanel({ focusTyped, retainAudio }: { focusTyped: () => void; retainAudio: boolean }) {
  const conv = useConversation();
  const speech = useSpeech();
  const [phase, setPhase] = useState<Phase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<VoiceErr | null>(null);
  const [level, setLevel] = useState(0);
  const recorder = useRef<AudioRecorder | null>(null);
  const vad = useRef<SilenceDetector | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  const meta = useRef<{ speechPlaying: boolean; handsFree: boolean }>({ speechPlaying: false, handsFree: false });
  const holdStart = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const stopRequested = useRef(false);
  const startToken = useRef(0);

  // Hands-free session
  const [hf, setHf] = useState<{ id: string; endsAt: number; maxS: number } | null>(null);
  const [hfRemaining, setHfRemaining] = useState(0);
  const [hfNote, setHfNote] = useState<string | null>(null);
  const hfRef = useRef(hf);
  hfRef.current = hf;

  const capability = micCapabilityProblem();

  useEffect(() => {
    if (phase !== 'recording') return;
    const id = window.setInterval(() => setElapsed(recorder.current?.elapsedMs ?? 0), 200);
    return () => window.clearInterval(id);
  }, [phase]);

  const stopVad = () => {
    vad.current?.stop();
    vad.current = null;
    setLevel(0);
  };

  const upload = useCallback(
    async (rec: Recording) => {
      stopVad();
      setPhase('uploading');
      setErr(null);
      try {
        const conversation_id = await conv.ensureConversation();
        const form = new FormData();
        form.append('file', rec.blob, `capture.${extensionFor(rec.mime)}`);
        form.append('captured_at', rec.capturedAt);
        form.append('tz', rec.tz);
        form.append('conversation_id', conversation_id);
        if (meta.current.handsFree && hfRef.current) form.append('voice_session_id', hfRef.current.id);
        form.append('retain_audio', retainAudio ? 'true' : 'false');
        const ac = new AbortController();
        uploadAbort.current = ac;
        const capture = await uploadWithProgress<Capture>('/api/captures/audio', form, {
          signal: ac.signal,
          onUploaded: () => setPhase('transcribing'),
        });
        uploadAbort.current = null;
        if (capture.error) {
          setErr({ kind: 'audio', code: capture.error.code, message: AUDIO_ERROR_TEXT[capture.error.code as AudioErrorCode] ?? capture.error.message });
          setPhase('idle');
          return;
        }
        const d: Draft = {
          capture,
          text: capture.transcript?.text ?? '',
          capturedAt: rec.capturedAt,
          tz: rec.tz,
          speechPlaying: meta.current.speechPlaying,
          handsFree: meta.current.handsFree,
        };
        setDraft(d);
        setPhase('review');
        const conf = capture.transcript?.confidence;
        if (d.handsFree && d.text.trim() && (conf === null || conf === undefined || conf >= LOW_CONFIDENCE)) {
          await sendDraft(d);
        } else if (d.handsFree) {
          setHfNote('Low-confidence transcript — review it before sending. Hands-free listening resumes after you send or discard.');
        }
      } catch (e) {
        uploadAbort.current = null;
        if (e instanceof DOMException && e.name === 'AbortError') {
          setPhase('idle');
          return;
        }
        if (e instanceof ApiError && e.isAudioError) {
          const code = e.audioCode!;
          setErr({ kind: 'audio', code, message: AUDIO_ERROR_TEXT[code] ?? e.message });
          // In hands-free mode, "no speech" just means keep listening.
          if (code === 'no_speech' && meta.current.handsFree && hfRef.current) setErr(null);
        } else {
          setErr({ kind: 'upload', code: e instanceof ApiError ? String(e.code ?? e.status) : 'error', message: `Upload failed: ${errorMessage(e)}` });
          if (hfRef.current) void endHandsFree('Stopped hands-free after an upload error.');
        }
        setPhase('idle');
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conv.ensureConversation, retainAudio],
  );

  const start = useCallback(
    async (opts: { handsFree?: boolean } = {}) => {
      if (phase !== 'idle' && phase !== 'review') return;
      setErr(null);
      setDraft(null);
      setElapsed(0);
      stopRequested.current = false;
      // Record whether Jarvis was audible, then silence it to avoid self-transcription.
      meta.current = { speechPlaying: tts.isSpeaking(), handsFree: !!opts.handsFree };
      speech.stop();
      setPhase('starting');
      const rec = new AudioRecorder(MAX_RECORDING_S);
      recorder.current = rec;
      const token = ++startToken.current;
      try {
        await rec.start({
          onAutoStop: (r) => void upload(r),
          onStream: (stream) => {
            if (opts.handsFree && SilenceDetector.supported()) {
              const det = new SilenceDetector({
                silenceMs: 1500,
                onLevel: (l) => setLevel(l),
                onSilence: () => void stop(),
                onNoSpeech: () => {
                  recorder.current?.cancel();
                  stopVad();
                  setPhase('idle');
                },
              });
              det.start(stream);
              vad.current = det;
            }
          },
        });
        if (token !== startToken.current) {
          // Cancelled while the microphone was starting: discard.
          rec.cancel();
          stopVad();
          return;
        }
        setPhase('recording');
        if (stopRequested.current) {
          stopRequested.current = false;
          void stop();
        }
      } catch (e) {
        if (token !== startToken.current) return;
        recorder.current = null;
        setPhase('idle');
        if (e instanceof MicError) {
          setErr({ kind: 'mic', code: e.kind, message: e.message });
          if (hfRef.current) void endHandsFree(null);
          focusTyped();
        } else {
          setErr({ kind: 'mic', code: 'unknown', message: errorMessage(e) });
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [phase, upload, focusTyped],
  );

  const stop = useCallback(async () => {
    const rec = recorder.current;
    if (!rec) return;
    if (!rec.recording) {
      // Released before the mic finished starting: stop as soon as it is live.
      stopRequested.current = true;
      return;
    }
    const r = await rec.stop();
    if (recorder.current === rec) recorder.current = null;
    if (r) await upload(r);
    else setPhase('idle');
  }, [upload]);

  const cancel = useCallback(() => {
    startToken.current++;
    stopRequested.current = false;
    recorder.current?.cancel();
    recorder.current = null;
    uploadAbort.current?.abort();
    stopVad();
    setPhase('idle');
    setElapsed(0);
  }, []);

  const sendDraft = useCallback(
    async (d: Draft) => {
      setPhase('sending');
      try {
        let version = d.capture.transcript?.version;
        const original = d.capture.transcript?.text ?? '';
        if (d.text.trim() !== original.trim()) {
          const edited = await api.editTranscript(d.capture.capture_id, d.text.trim());
          version = edited.transcript?.version ?? version;
        }
        setDraft(null);
        setHfNote(null);
        await conv.send({
          text: d.text.trim(),
          channel: 'voice',
          capture_id: d.capture.capture_id,
          transcript_version: version,
          captured_at: d.capturedAt,
          tz: d.tz,
          speech_playing: d.speechPlaying,
        });
        setPhase('idle');
      } catch (e) {
        setErr({ kind: 'upload', code: 'transcript', message: `Could not save the edited transcript: ${errorMessage(e)}` });
        setPhase('review');
      }
    },
    [conv],
  );

  // ----- Hands-free session control -----
  const endHandsFree = useCallback(async (note: string | null) => {
    const cur = hfRef.current;
    setHf(null);
    hfRef.current = null;
    startToken.current++;
    recorder.current?.cancel();
    recorder.current = null;
    stopVad();
    setPhase((p) => (p === 'recording' || p === 'starting' ? 'idle' : p));
    setHfNote(note);
    if (cur) await api.endVoiceSession(cur.id).catch(() => undefined);
  }, []);

  const startHandsFree = async () => {
    setHfNote(null);
    setErr(null);
    try {
      const conversation_id = await conv.ensureConversation();
      const s = await api.startVoiceSession('hands_free', conversation_id);
      setHf({ id: s.id, endsAt: Date.now() + s.max_duration_s * 1000, maxS: s.max_duration_s });
      setHfRemaining(s.max_duration_s);
    } catch (e) {
      setHfNote(`Could not start a hands-free session: ${errorMessage(e)}`);
    }
  };

  useEffect(() => {
    if (!hf) return;
    const id = window.setInterval(() => {
      const rem = Math.max(0, Math.round((hf.endsAt - Date.now()) / 1000));
      setHfRemaining(rem);
      if (rem <= 0) void endHandsFree('Hands-free session ended: time limit reached.');
    }, 500);
    const onHide = () => {
      if (document.visibilityState === 'hidden') void endHandsFree('Hands-free session stopped because JARVIS left the foreground.');
    };
    document.addEventListener('visibilitychange', onHide);
    return () => {
      window.clearInterval(id);
      document.removeEventListener('visibilitychange', onHide);
    };
  }, [hf, endHandsFree]);

  // Listening loop: only when idle, not speaking, and not sending a command.
  useEffect(() => {
    if (!hf || phase !== 'idle' || speech.speaking || conv.sending || err?.kind === 'mic') return;
    const t = window.setTimeout(() => void start({ handsFree: true }), 400);
    return () => window.clearTimeout(t);
  }, [hf, phase, speech.speaking, conv.sending, err, start]);

  // Unmount: release the mic and end any session.
  useEffect(
    () => () => {
      recorder.current?.cancel();
      vad.current?.stop();
      uploadAbort.current?.abort();
      const cur = hfRef.current;
      if (cur) void api.endVoiceSession(cur.id).catch(() => undefined);
    },
    [],
  );

  // ----- Record button gestures: hold = push-to-talk; tap = start/stop -----
  const onPointerDown = (e: React.PointerEvent) => {
    suppressClick.current = false;
    if (e.button !== 0 || hf) return;
    if (phase === 'idle' || phase === 'review') {
      holdStart.current = Date.now();
      suppressClick.current = true;
      void start();
    }
  };
  const onPointerUp = () => {
    if (holdStart.current !== null) {
      const held = Date.now() - holdStart.current;
      holdStart.current = null;
      if (held >= HOLD_MS) void stop();
    }
  };
  const onClick = () => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    if (hf) return;
    if (phase === 'recording') void stop();
    else if (phase === 'idle' || phase === 'review') void start();
  };

  const recording = phase === 'recording';
  const busy = phase === 'uploading' || phase === 'transcribing' || phase === 'sending';
  const micDisabledReason = capability ? capability.message : null;
  const t = draft?.capture.transcript;

  return (
    <section className="voice" aria-label="Voice capture">
      <div className="voice-main">
        <button
          type="button"
          className={`record-btn ${recording ? 'recording' : ''}`}
          aria-pressed={recording}
          aria-label={recording ? 'Stop recording and transcribe' : 'Record voice command (tap to start, or hold to talk)'}
          data-testid="record-btn"
          disabled={!!capability || busy || !!hf}
          onPointerDown={onPointerDown}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
          onContextMenu={(e) => e.preventDefault()}
          onClick={onClick}
        >
          <span className="record-icon" aria-hidden="true">
            {recording ? '■' : '●'}
          </span>
          <span className="record-label">{recording ? 'Tap to stop' : phase === 'starting' ? 'Starting mic…' : 'Tap or hold to talk'}</span>
        </button>

        <div className="voice-status" aria-live="polite" data-testid="voice-status">
          {recording && (
            <div className="rec-state">
              <span className="rec-dot" aria-hidden="true" /> Recording <strong className="timer">{fmtElapsed(elapsed)}</strong>
              <span className="muted"> / {fmtElapsed(MAX_RECORDING_S * 1000)} max</span>
              {hf && <span className="level" style={{ ['--lvl' as string]: Math.min(1, level * 12) }} aria-hidden="true" />}
            </div>
          )}
          {phase === 'uploading' && <span>Uploading audio…</span>}
          {phase === 'transcribing' && <span>Transcribing locally…</span>}
          {phase === 'sending' && <span>Sending command…</span>}
          {phase === 'idle' && !err && !hf && <span className="muted hide-mobile">Hold to talk, or tap once to start and again to stop.</span>}
          {micDisabledReason && !hf && <span className="warn-text">{micDisabledReason}</span>}
        </div>

        {(recording || phase === 'starting') && (
          <div className="rec-controls">
            <button type="button" className="btn btn-primary btn-large" onClick={() => void stop()} disabled={phase !== 'recording'}>
              Stop &amp; transcribe
            </button>
            <button type="button" className="btn btn-large" onClick={cancel}>
              Cancel (discard)
            </button>
          </div>
        )}
        {(phase === 'uploading' || phase === 'transcribing') && (
          <div className="rec-controls">
            <button type="button" className="btn" onClick={cancel}>
              Cancel
            </button>
          </div>
        )}
      </div>

      {err && (
        <div className={`voice-error notice notice-${err.kind === 'mic' ? 'warn' : 'bad'}`} role="alert" data-testid="voice-error">
          <strong>{err.kind === 'mic' ? 'Microphone unavailable' : err.kind === 'audio' ? `Voice error (${err.code})` : 'Voice upload error'}</strong>
          <span>{err.message}</span>
          <span className="muted small">This is a voice/transcription problem, not a reasoning error. Typed commands still work.</span>
          <button type="button" className="btn btn-small" onClick={() => setErr(null)}>
            Dismiss
          </button>
        </div>
      )}

      {draft && (phase === 'review' || phase === 'sending') && (
        <form
          className="transcript"
          data-testid="transcript-review"
          onSubmit={(e) => {
            e.preventDefault();
            void sendDraft(draft);
          }}
        >
          <label htmlFor="transcript-text">
            Transcript <span className="muted">(edit before sending)</span>
          </label>
          <textarea id="transcript-text" rows={3} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} data-testid="transcript-text" />
          <div className="transcript-meta">
            {t ? (
              <>
                <span className={t.confidence !== null && t.confidence < LOW_CONFIDENCE ? 'warn-text' : ''}>
                  Confidence {t.confidence === null ? 'n/a' : `${Math.round(t.confidence * 100)}%`}
                </span>
                <span>Latency {t.latency_ms === null ? 'n/a' : `${t.latency_ms} ms`}</span>
                {t.model && <span>{t.model}</span>}
                <span>v{t.version}</span>
              </>
            ) : (
              <span className="warn-text">No transcript returned (status: {draft.capture.status}).</span>
            )}
            {draft.capture.audio?.duration_s != null && <span>{draft.capture.audio.duration_s.toFixed(1)} s audio</span>}
          </div>
          {t && t.confidence !== null && t.confidence < LOW_CONFIDENCE && <p className="warn-text small">Low confidence — check the words before sending.</p>}
          <div className="row gap">
            <button type="submit" className="btn btn-primary" disabled={!draft.text.trim() || phase === 'sending'} data-testid="send-transcript">
              {phase === 'sending' ? 'Sending…' : 'Send as command'}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setDraft(null);
                setPhase('idle');
                setHfNote(null);
              }}
            >
              Discard
            </button>
          </div>
        </form>
      )}

      <div className="handsfree">
        {hf ? (
          <div className="handsfree-active" role="status">
            <strong>Hands-free session (foreground only)</strong>
            <span>
              {speech.speaking ? 'Paused while JARVIS speaks' : phase === 'recording' ? 'Listening — stops after ~1.5 s of silence' : phase === 'idle' ? 'Getting ready…' : 'Processing…'}
            </span>
            <span className="timer">Ends in {fmtElapsed(hfRemaining * 1000)}</span>
            <button type="button" className="btn btn-danger" onClick={() => void endHandsFree('Hands-free session stopped.')}>
              Stop hands-free
            </button>
          </div>
        ) : (
          <div className="handsfree-idle">
            <button
              type="button"
              className="btn"
              disabled={!!capability || phase !== 'idle' || !SilenceDetector.supported()}
              onClick={() => void startHandsFree()}
              title={capability ? capability.message : undefined}
            >
              Start hands-free session
            </button>
            <span className="hint hide-mobile">
              Foreground conversation with automatic silence detection and a time limit. No wake word; stops if you leave the app.
              {!SilenceDetector.supported() && ' Not supported in this browser (no Web Audio).'}
            </span>
          </div>
        )}
        {hfNote && <p className="muted small">{hfNote}</p>}
      </div>
    </section>
  );
}
