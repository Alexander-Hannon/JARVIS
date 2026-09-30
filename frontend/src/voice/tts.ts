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
export function unlockAudio(): void {
  if (unlocked || typeof window === 'undefined') return;
  unlocked = true;
  const a = audioEl();
  a.src = SILENT_WAV;
  a.play().catch(() => {
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
    this.abort = new AbortController();
    this.speaking = true;
    this.emit();
    fetchTtsAudio(text, preset, this.abort.signal, opts.rate ?? 1)
      .then((blob) => {
        if (my !== this.seq) return;
        const url = URL.createObjectURL(blob);
        const a = audioEl();
        a.src = url;
        a.playbackRate = 1; // speed is synthesized server-side (natural pitch)
        const done = () => {
          URL.revokeObjectURL(url);
          if (my === this.seq) {
            this.audio = null;
            this.speaking = false;
            this.emit();
          }
        };
        a.onended = done;
        a.onerror = done;
        this.audio = a;
        return a.play().catch(done);
      })
      .catch((e: unknown) => {
        if (my !== this.seq || (e instanceof DOMException && e.name === 'AbortError')) return;
        this.speaking = false;
        this.fallback.speak(text, { rate: opts.rate }); // local voice unavailable → browser voice
      });
  }

  stop(): void {
    this.seq++;
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
