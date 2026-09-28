// Real browser microphone capture via getUserMedia + MediaRecorder.
// Container/codec is feature-detected; iOS Safari records audio/mp4, Chromium WebM/Opus.

export const MAX_RECORDING_S = 300;

export const CANDIDATE_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/aac',
  'audio/ogg;codecs=opus',
] as const;

export type MicErrorKind = 'permission_denied' | 'no_device' | 'insecure_context' | 'unsupported' | 'device_busy' | 'unknown';

export class MicError extends Error {
  readonly kind: MicErrorKind;
  constructor(kind: MicErrorKind, message: string) {
    super(message);
    this.name = 'MicError';
    this.kind = kind;
  }
}

/** Returns the first supported mime type, or '' to let the browser choose its default. */
export function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') return '';
  for (const t of CANDIDATE_MIME_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(t)) return t;
    } catch {
      /* some engines throw for unknown types */
    }
  }
  return '';
}

export function extensionFor(mime: string): string {
  const m = mime.toLowerCase();
  if (m.includes('webm')) return 'webm';
  if (m.includes('mp4') || m.includes('m4a')) return 'm4a';
  if (m.includes('aac')) return 'aac';
  if (m.includes('ogg')) return 'ogg';
  if (m.includes('wav')) return 'wav';
  return 'bin';
}

/** Checks capability without prompting. Returns a MicError describing why capture is impossible, or null. */
export function micCapabilityProblem(): MicError | null {
  const md = typeof navigator !== 'undefined' ? navigator.mediaDevices : undefined;
  if (!md || typeof md.getUserMedia !== 'function') {
    if (typeof window !== 'undefined' && window.isSecureContext === false) {
      return new MicError(
        'insecure_context',
        'The microphone is only available over HTTPS or on localhost. Open JARVIS via https:// (or http://localhost on this machine) to record. Typed commands still work.',
      );
    }
    return new MicError('unsupported', 'This browser does not expose microphone capture (getUserMedia). Typed commands still work.');
  }
  if (typeof MediaRecorder === 'undefined') {
    return new MicError('unsupported', 'This browser cannot record audio (MediaRecorder is missing). Typed commands still work.');
  }
  return null;
}

export function classifyGetUserMediaError(e: unknown): MicError {
  const name = e instanceof DOMException || e instanceof Error ? e.name : '';
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return new MicError(
        'permission_denied',
        'Microphone permission was denied. Allow microphone access for this site in your browser settings to use voice — you can keep typing commands below.',
      );
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return new MicError('no_device', 'No microphone was found. Connect a microphone or keep typing commands below.');
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return new MicError('device_busy', 'The microphone is in use by another app or could not be started. Close other apps using it and try again.');
    default:
      return new MicError('unknown', `Could not start the microphone${e instanceof Error && e.message ? `: ${e.message}` : '.'}`);
  }
}

export interface Recording {
  blob: Blob;
  mime: string;
  durationMs: number;
  capturedAt: string; // ISO UTC, when recording started
  tz: string;
}

export interface RecorderCallbacks {
  /** Fired when the recorder stops itself (max duration). */
  onAutoStop?: (rec: Recording) => void;
  /** Receives the live stream so VAD can attach. */
  onStream?: (stream: MediaStream) => void;
}

export class AudioRecorder {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private startedAt = 0;
  private capturedAt = '';
  private maxTimer: number | null = null;
  private stopResolve: ((r: Recording | null) => void) | null = null;
  private cancelled = false;
  readonly maxSeconds: number;

  constructor(maxSeconds = MAX_RECORDING_S) {
    this.maxSeconds = maxSeconds;
  }

  get recording(): boolean {
    return this.recorder !== null && this.recorder.state === 'recording';
  }
  get elapsedMs(): number {
    return this.startedAt ? Date.now() - this.startedAt : 0;
  }
  get mediaStream(): MediaStream | null {
    return this.stream;
  }

  async start(cb: RecorderCallbacks = {}): Promise<void> {
    const problem = micCapabilityProblem();
    if (problem) throw problem;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      throw classifyGetUserMediaError(e);
    }
    this.stream = stream;
    const mime = pickMimeType();
    let rec: MediaRecorder;
    try {
      rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      rec = new MediaRecorder(stream);
    }
    this.recorder = rec;
    this.chunks = [];
    this.cancelled = false;
    rec.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) this.chunks.push(ev.data);
    };
    rec.onstop = () => {
      const recording = this.cancelled ? null : this.buildRecording();
      this.releaseStream();
      const resolve = this.stopResolve;
      this.stopResolve = null;
      if (resolve) resolve(recording);
      else if (recording && cb.onAutoStop) cb.onAutoStop(recording);
    };
    this.startedAt = Date.now();
    this.capturedAt = new Date(this.startedAt).toISOString();
    rec.start(250);
    cb.onStream?.(stream);
    this.maxTimer = window.setTimeout(() => {
      if (this.recording) this.recorder?.stop();
    }, this.maxSeconds * 1000);
  }

  private buildRecording(): Recording {
    const mime = this.recorder?.mimeType || this.chunks[0]?.type || 'application/octet-stream';
    return {
      blob: new Blob(this.chunks, { type: mime }),
      mime,
      durationMs: Date.now() - this.startedAt,
      capturedAt: this.capturedAt,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };
  }

  private releaseStream(): void {
    if (this.maxTimer !== null) window.clearTimeout(this.maxTimer);
    this.maxTimer = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.recorder = null;
    this.startedAt = 0;
  }

  /** Stops and returns the recording (null if nothing was recorded). */
  stop(): Promise<Recording | null> {
    const rec = this.recorder;
    if (!rec || rec.state === 'inactive') {
      this.releaseStream();
      return Promise.resolve(null);
    }
    return new Promise((resolve) => {
      this.stopResolve = resolve;
      rec.stop();
    });
  }

  /** Discards the recording; nothing is uploaded. */
  cancel(): void {
    this.cancelled = true;
    const rec = this.recorder;
    if (rec && rec.state !== 'inactive') {
      this.stopResolve = null;
      rec.stop();
    } else {
      this.releaseStream();
    }
    this.chunks = [];
  }
}
