// RMS-based silence detection with Web Audio AnalyserNode.
// Fires onSilence once the user has spoken and then stayed below threshold for `silenceMs`.

export interface VadOptions {
  silenceMs?: number;
  /** RMS level (0..1) above which we count as speech. */
  speechThreshold?: number;
  /** Give up waiting for any speech after this long (fires onNoSpeech). */
  noSpeechTimeoutMs?: number;
  onSilence: () => void;
  onNoSpeech?: () => void;
  onLevel?: (rms: number) => void;
}

export class SilenceDetector {
  private ctx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private analyser: AnalyserNode | null = null;
  private raf: number | null = null;
  private interval: number | null = null;
  private heardSpeech = false;
  private silentSince: number | null = null;
  private startedAt = 0;
  private fired = false;

  constructor(private readonly opts: VadOptions) {}

  static supported(): boolean {
    return typeof window !== 'undefined' && (typeof window.AudioContext !== 'undefined' || 'webkitAudioContext' in window);
  }

  start(stream: MediaStream): void {
    const Ctor: typeof AudioContext =
      window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    this.ctx = new Ctor();
    this.source = this.ctx.createMediaStreamSource(stream);
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 2048;
    this.source.connect(this.analyser);
    this.startedAt = performance.now();
    const buf = new Float32Array(this.analyser.fftSize);
    const silenceMs = this.opts.silenceMs ?? 1500;
    const threshold = this.opts.speechThreshold ?? 0.02;
    const noSpeech = this.opts.noSpeechTimeoutMs ?? 12000;
    // setInterval rather than rAF: rAF halts in background tabs, and the session must not run hidden anyway.
    this.interval = window.setInterval(() => {
      if (!this.analyser || this.fired) return;
      this.analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      this.opts.onLevel?.(rms);
      const now = performance.now();
      if (rms >= threshold) {
        this.heardSpeech = true;
        this.silentSince = null;
      } else if (this.heardSpeech) {
        if (this.silentSince === null) this.silentSince = now;
        else if (now - this.silentSince >= silenceMs) {
          this.fired = true;
          this.opts.onSilence();
        }
      } else if (now - this.startedAt >= noSpeech) {
        this.fired = true;
        this.opts.onNoSpeech?.();
      }
    }, 50);
  }

  stop(): void {
    if (this.interval !== null) window.clearInterval(this.interval);
    if (this.raf !== null) cancelAnimationFrame(this.raf);
    this.interval = null;
    try {
      this.source?.disconnect();
    } catch {
      /* ignore */
    }
    void this.ctx?.close().catch(() => undefined);
    this.ctx = null;
    this.source = null;
    this.analyser = null;
  }
}
