import { useEffect, useId, useState } from 'react';
import { useSpeech } from '../state/app';
import { tts } from '../voice/tts';

export function SpeechControls({ full = false }: { full?: boolean }) {
  const s = useSpeech();
  const [voices, setVoices] = useState(() => tts.voices());
  const vid = useId();
  const rid = useId();
  useEffect(() => tts.subscribe(() => setVoices(tts.voices())), []);

  if (!s.available) return <p className="muted small">Speech output is not supported in this browser. Responses are shown as text.</p>;

  return (
    <div className={`speech-controls ${full ? 'full' : ''}`} aria-label="Speech output">
      <button type="button" className="btn btn-small" onClick={s.stop} disabled={!s.speaking} title={s.speaking ? undefined : 'Nothing is playing'}>
        Stop speaking
      </button>
      <button type="button" className="btn btn-small" onClick={s.replay} disabled={!s.lastSpoken || s.prefs.muted} title={!s.lastSpoken ? 'Nothing spoken yet' : s.prefs.muted ? 'Unmute to replay' : undefined}>
        Replay last
      </button>
      <button type="button" className="btn btn-small" aria-pressed={s.prefs.muted} onClick={() => s.setPrefs({ muted: !s.prefs.muted })}>
        {s.prefs.muted ? 'Unmute voice' : 'Mute voice'}
      </button>
      {full && (
        <>
          <label htmlFor={vid} className="small">
            Voice
          </label>
          <select id={vid} value={s.prefs.voiceName ?? ''} onChange={(e) => s.setPrefs({ voiceName: e.target.value || null })}>
            <optgroup label="Jarvis voices (local, private)">
              {voices.filter((v) => v.label).map((v) => (
                <option key={v.name} value={v.name}>
                  {v.label}
                </option>
              ))}
            </optgroup>
            <optgroup label="Browser voices">
              <option value="">System default</option>
              {voices.filter((v) => !v.label).map((v) => (
                <option key={v.name} value={v.name}>
                  {v.name} ({v.lang})
                </option>
              ))}
            </optgroup>
          </select>
          <label htmlFor={rid} className="small">
            Rate {s.prefs.rate.toFixed(1)}×
          </label>
          <input id={rid} type="range" min={0.6} max={1.6} step={0.1} value={s.prefs.rate} onChange={(e) => s.setPrefs({ rate: Number(e.target.value) })} />
          <button type="button" className="btn btn-small" onClick={() => s.speak('Good evening. All systems are online, and I am at your service.')} disabled={s.prefs.muted}>
            Test voice
          </button>
        </>
      )}
    </div>
  );
}
