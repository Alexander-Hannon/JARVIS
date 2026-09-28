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
  voices(): { name: string; lang: string; default: boolean }[];
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

export const tts: TtsProvider = new BrowserSpeechTts();

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
      const p = JSON.parse(raw) as Partial<VoicePrefs>;
      return { voiceName: p.voiceName ?? null, rate: typeof p.rate === 'number' ? p.rate : 1, muted: !!p.muted };
    }
  } catch {
    /* ignore */
  }
  return { voiceName: null, rate: 1, muted: false };
}
export function saveVoicePrefs(p: VoicePrefs): void {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}
