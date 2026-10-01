import { fetchTtsAudio } from '../api/client';

// Speech output behind a small provider interface so another TTS engine can replace it.

export interface TtsOptions {
  voiceName?: string | null;
  rate?: number;
}

export interface TtsProvider {
  readonly name: string;
  readonly available: boolean;
  speak(text: string, opts?: TtsOptions): void;
  stop(): void;
  isSpeaking(): boolean;
  /** Text currently being spoken (live captions); empty when silent. */
  readonly caption?: string;
  voices(): { name: string; lang: string; default: boolean; label?: string }[];
  /** Subscribe to speaking-state / voice-list changes. Returns an unsubscribe fn. */
  subscribe(fn: () => void): () => void;
}

class BrowserSpeechTts implements TtsProvider {
  readonly name = 'browser-speechSynthesis';
  private listeners = new Set<() => void>();
  private speaking = false;
  private current: SpeechSynthesisUtterance | null = null;

  constructor() {
    if (this.available) {
      const synth = window.speechSynthesis;
      const notify = () => this.emit();
      if (typeof synth.addEventListener === 'function') synth.addEventListener('voiceschanged', notify);
      else synth.onvoiceschanged = notify;
    }
  }

  get available(): boolean {
    return typeof window !== 'undefined' && 'speechSynthesis' in window && typeof SpeechSynthesisUtterance !== 'undefined';
  }

  private emit(): void {
    this.listeners.forEach((l) => l());
  }

  voices() {
    if (!this.available) return [];
    return window.speechSynthesis.getVoices().map((v) => ({ name: v.name, lang: v.lang, default: v.default }));
  }

  speak(text: string, opts: TtsOptions = {}): void {
    if (!this.available || !text.trim()) return;
    const synth = window.speechSynthesis;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    if (opts.voiceName) {
      const v = synth.getVoices().find((x) => x.name === opts.voiceName);
      if (v) u.voice = v;
    }
    u.rate = opts.rate ?? 1;
    const done = () => {
      if (this.current === u) {
        this.current = null;
        this.speaking = false;
        this.emit();
      }
    };
    u.onend = done;
    u.onerror = done;
    this.current = u;
    this.speaking = true;
    this.emit();
    synth.speak(u);
  }

  stop(): void {
    if (!this.available) return;
    window.speechSynthesis.cancel();
    this.current = null;
    if (this.speaking) {
      this.speaking = false;
      this.emit();
    }
  }

  isSpeaking(): boolean {
    if (!this.available) return false;
    return this.speaking || window.speechSynthesis.speaking;
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
}

/** Never read markdown / symbols aloud (server already cleans spoken text; this is the client-side safety net). */
export function speakable(text: string): string {
  return text
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\[[^\]]*·[^\]]*\]/g, '')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/^\s*#{1,6}\s*/gm, '')
    .replace(/^\s*(?:[-*+•▸⚑]|\d+[.)])\s+/gm, '')
    .replace(/[*#_`|>~▸⚑•→]+/g, ' ')
    .replace(/°F/g, ' degrees')
    .replace(/\s*\n\s*/g, '. ')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

/** Split speech into sentence groups (~240 chars) so playback can start quickly and stream the rest. */
export function splitForSpeech(text: string, max = 240): string[] {
  const sentences = text.match(/[^.!?]+(?:[.!?]+|$)/g)?.map((x) => x.trim()).filter(Boolean) ?? [text];
  const out: string[] = [];
  let cur = '';
  for (const s of sentences) {
    if (s.length > max * 1.6) {
      // very long sentence: break on commas / spaces
      if (cur) {
        out.push(cur);
        cur = '';
      }
      let rest = s;
      while (rest.length > max) {
        let cut = rest.lastIndexOf(', ', max);
        if (cut < max / 2) cut = rest.lastIndexOf(' ', max);
        if (cut < 1) cut = max;
        out.push(rest.slice(0, cut + 1).trim());
        rest = rest.slice(cut + 1);
      }
      cur = rest.trim();
    } else if ((cur + ' ' + s).trim().length > max && cur) {
      out.push(cur);
      cur = s;
    } else {
      cur = (cur + ' ' + s).trim();
    }
  }
  if (cur) out.push(cur);
  // first chunk short → Jarvis starts talking sooner
  if (out.length && out[0].length > 140) {
    const first = out[0].match(/^[^.!?]+[.!?]+/);
    if (first && first[0].length < out[0].length) out.splice(0, 1, first[0].trim(), out[0].slice(first[0].length).trim());
  }
  return out.filter(Boolean);
}

/** Voice names starting with this prefix are local Piper presets served by the Jarvis backend. */
export const JARVIS_VOICE_PREFIX = 'jarvis:';
export const JARVIS_PRESETS = [
  { key: 'jarvis', label: 'Jarvis — refined British (local, default)' },
  { key: 'jarvis-dry', label: 'Jarvis — refined British, no effect (local)' },
  { key: 'obadiah', label: 'Obadiah — deep British (local)' },
  { key: 'northern', label: 'Northern English male (local)' },
];

/**
 * One persistent <audio> element, "unlocked" by the first user gesture. Mobile browsers (iOS especially) block
 * audio started after a network wait unless the element was already played from a tap — this makes auto-speak work.
 */
let sharedAudio: HTMLAudioElement | null = null;
function audioEl(): HTMLAudioElement {
  if (!sharedAudio) {
    sharedAudio = new Audio();
    sharedAudio.preload = 'auto';
  }
  return sharedAudio;
}
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
let unlocked = false;

/*
 * Optional audio analysis for the voice ribbon. Routing an <audio> element through Web Audio means it is SILENT
 * if the context is suspended, so we only wire it once the context is confirmed running, and never on iOS
 * (where context state is unreliable) — there the ribbon animates procedurally instead.
 */
let ctx: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
const isIOS = typeof navigator !== 'undefined' && /iPhone|iPad|iPod/.test(navigator.userAgent);
async function ensureAnalyser(): Promise<void> {
  if (analyser || isIOS || typeof window === 'undefined' || !('AudioContext' in window)) return;
  try {
    ctx = ctx ?? new AudioContext();
    if (ctx.state !== 'running') await ctx.resume();
    if (ctx.state !== 'running') return;
    const src = ctx.createMediaElementSource(audioEl());
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    an.smoothingTimeConstant = 0.6;
    src.connect(an);
    an.connect(ctx.destination);
    analyser = an;
  } catch {
    analyser = null;
  }
}
/** Live waveform of Jarvis's voice (null when unavailable — callers animate procedurally). */
export function voiceWaveform(buf: Uint8Array<ArrayBuffer>): boolean {
  if (!analyser) return false;
  analyser.getByteTimeDomainData(buf);
  return true;
}
export function unlockAudio(): void {
  if (unlocked || typeof window === 'undefined') return;
  unlocked = true;
  const a = audioEl();
  a.src = SILENT_WAV;
  a.play()
    .then(() => void ensureAnalyser())
    .catch(() => {
      unlocked = false;
    });
  // Note: do NOT prime speechSynthesis here — an utterance in flight would mark Jarvis as "speaking" at mic start,
  // and approvals captured while Jarvis speaks are (correctly) refused.
}
if (typeof window !== 'undefined') {
  const once = () => unlockAudio();
  window.addEventListener('pointerdown', once, { capture: true, passive: true });
  window.addEventListener('keydown', once, { capture: true });
}

/** Local neural voice (Piper on the Jarvis host) with automatic fallback to the browser voice. */
class JarvisTts implements TtsProvider {
  readonly name = 'jarvis-local-neural';
  private listeners = new Set<() => void>();
  private audio: HTMLAudioElement | null = null;
  private abort: AbortController | null = null;
  private speaking = false;
  private seq = 0;
  /** The sentence(s) being spoken right now — shown as live captions. */
  caption = '';

  constructor(private fallback: BrowserSpeechTts) {
    fallback.subscribe(() => this.emit());
  }

  get available(): boolean {
    return typeof window !== 'undefined' && typeof Audio !== 'undefined';
  }

  private emit(): void {
    this.listeners.forEach((l) => l());
  }

  voices() {
    return [
      ...JARVIS_PRESETS.map((p, i) => ({ name: JARVIS_VOICE_PREFIX + p.key, lang: 'en-GB', default: i === 0, label: p.label })),
      ...this.fallback.voices(),
    ];
  }

  speak(raw: string, opts: TtsOptions = {}): void {
    const text = speakable(raw);
    if (!text.trim()) return;
    this.stop();
    const name = opts.voiceName ?? JARVIS_VOICE_PREFIX + 'jarvis';
    if (!name.startsWith(JARVIS_VOICE_PREFIX)) {
      this.fallback.speak(text, opts);
      return;
    }
    const preset = name.slice(JARVIS_VOICE_PREFIX.length);
    const my = ++this.seq;
    const ac = new AbortController();
    this.abort = ac;
    this.speaking = true;
    this.emit();
    const chunks = splitForSpeech(text);
    const rate = opts.rate ?? 1;
    this.caption = chunks[0] ?? '';
    this.emit();
    // Pipeline: fetch chunk i+1 while chunk i plays → starts fast and never truncates long replies.
    const fetchChunk = (i: number) => fetchTtsAudio(chunks[i], preset, ac.signal, rate);
    const playBlob = (blob: Blob) =>
      new Promise<void>((resolve) => {
        const url = URL.createObjectURL(blob);
        const a = audioEl();
        a.src = url;
        a.playbackRate = 1; // speed is synthesized server-side (natural pitch)
        const done = () => {
          URL.revokeObjectURL(url);
          resolve();
        };
        a.onended = done;
        a.onerror = done;
        this.audio = a;
        a.play().catch(done);
      });
    const finish = () => {
      if (my === this.seq) {
        this.audio = null;
        this.speaking = false;
        this.emit();
      }
    };
    (async () => {
      let next = fetchChunk(0);
      for (let i = 0; i < chunks.length; i++) {
        let blob: Blob;
        try {
          blob = await next;
        } catch (e) {
          if (my !== this.seq || (e instanceof DOMException && e.name === 'AbortError')) return;
          // local voice unavailable → browser voice for whatever hasn't been spoken yet
          this.speaking = false;
          this.fallback.speak(chunks.slice(i).join(' '), { rate });
          return;
        }
        if (my !== this.seq) return;
        if (i + 1 < chunks.length) {
          next = fetchChunk(i + 1);
          next.catch(() => undefined);
        }
        this.caption = chunks[i];
        this.emit();
        await playBlob(blob);
        if (my !== this.seq) return;
      }
      this.caption = '';
      finish();
    })();
  }

  stop(): void {
    this.seq++;
    this.caption = '';
    this.abort?.abort();
    this.abort = null;
    if (this.audio) {
      this.audio.pause();
      this.audio = null;
    }
    this.fallback.stop();
    if (this.speaking) {
      this.speaking = false;
      this.emit();
    }
  }

  isSpeaking(): boolean {
    return this.speaking || this.fallback.isSpeaking();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
}

export const tts: TtsProvider = new JarvisTts(new BrowserSpeechTts());

// Per-device voice preferences (voice list differs per device, so localStorage is the right home;
// the backend copy via PATCH /api/settings is kept in sync as a default).
const PREF_KEY = 'jarvis.voicePrefs';
export interface VoicePrefs {
  voiceName: string | null;
  rate: number;
  muted: boolean;
}
export function loadVoicePrefs(): VoicePrefs {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<VoicePrefs> & { v?: number };
      // v2: the local Jarvis voice became the default; older saved prefs are migrated once.
      if (!p.v) p.voiceName = JARVIS_VOICE_PREFIX + 'jarvis';
      return { voiceName: p.voiceName ?? JARVIS_VOICE_PREFIX + 'jarvis', rate: typeof p.rate === 'number' ? p.rate : 1, muted: !!p.muted };
    }
  } catch {
    /* ignore */
  }
  return { voiceName: JARVIS_VOICE_PREFIX + 'jarvis', rate: 1, muted: false };
}
export function saveVoicePrefs(p: VoicePrefs): void {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ ...p, v: 2 }));
  } catch {
    /* ignore */
  }
}
