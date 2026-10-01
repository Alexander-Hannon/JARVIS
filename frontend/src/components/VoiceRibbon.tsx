import { useEffect, useRef, useState } from 'react';
import { tts, voiceWaveform } from '../voice/tts';

/**
 * Jarvis's presence: a waveform drawn from his actual voice output (or a procedural wave where the browser
 * can't analyse audio), plus live captions of the sentence being spoken. Still and quiet when he's silent.
 */
export function VoiceRibbon() {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const [speaking, setSpeaking] = useState(tts.isSpeaking());
  const [caption, setCaption] = useState(tts.caption ?? '');
  useEffect(
    () =>
      tts.subscribe(() => {
        setSpeaking(tts.isSpeaking());
        setCaption(tts.caption ?? '');
      }),
    [],
  );

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const g = c.getContext('2d');
    if (!g) return;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const buf = new Uint8Array(new ArrayBuffer(512));
    let raf = 0;
    let t = 0;
    let energy = 0;
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth;
      const h = c.clientHeight;
      if (c.width !== w * dpr || c.height !== h * dpr) {
        c.width = w * dpr;
        c.height = h * dpr;
      }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const mid = h / 2;
      let real = speaking && voiceWaveform(buf);
      if (real) {
        // analyser present but quiet (audio still loading / between chunks): fall back to the procedural wave
        let sum = 0;
        for (let i = 0; i < buf.length; i += 8) sum += Math.abs(buf[i] - 128);
        if (sum / (buf.length / 8) < 2) real = false;
      }
      const target = speaking ? 1 : 0;
      energy += (target - energy) * 0.08;
      t += 0.035;
      const pts = 96;
      const path = (amp: number, phase: number) => {
        g.beginPath();
        for (let i = 0; i <= pts; i++) {
          const x = (i / pts) * w;
          const edge = Math.sin((i / pts) * Math.PI); // taper at both ends
          let y: number;
          if (real) {
            const v = (buf[Math.floor((i / pts) * (buf.length - 1))] - 128) / 128;
            y = mid + v * h * 0.42 * edge * amp;
          } else {
            y = mid + Math.sin(i * 0.22 + t * 3 + phase) * Math.sin(i * 0.05 + t) * h * 0.28 * edge * energy * amp;
          }
          if (i === 0) g.moveTo(x, y);
          else g.lineTo(x, y);
        }
      };
      g.lineCap = 'round';
      g.shadowColor = 'rgba(79, 227, 255, 0.9)';
      g.shadowBlur = 12;
      g.strokeStyle = 'rgba(79, 227, 255, 0.35)';
      g.lineWidth = 1;
      path(0.6, 1.7);
      g.stroke();
      g.strokeStyle = speaking ? 'rgba(160, 240, 255, 0.95)' : 'rgba(79, 227, 255, 0.45)';
      g.lineWidth = speaking ? 2 : 1.25;
      path(1, 0);
      g.stroke();
      if (!reduce && (speaking || energy > 0.01)) raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [speaking]);

  return (
    <div className={`voice-ribbon ${speaking ? 'live' : ''}`} aria-live="polite">
      <canvas ref={canvas} aria-hidden="true" />
      <p className="ribbon-caption">{speaking && caption ? caption : speaking ? 'Speaking…' : 'Jarvis is listening for your next command.'}</p>
    </div>
  );
}
