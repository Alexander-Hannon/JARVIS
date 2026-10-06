import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useConversation } from '../state/app';
import { useNav } from './Layout';

/* Ctrl/⌘-K: jump anywhere, or send what you typed straight to JARVIS. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [sel, setSel] = useState(0);
  const nav = useNavigate();
  const allowed = useNav();
  const { send } = useConversation();
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => {
    if (open) {
      setText('');
      setSel(0);
      window.setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);

  const items = useMemo(() => {
    const t = text.trim().toLowerCase();
    const pages = allowed.filter((n) => !t || n.label.toLowerCase().includes(t)).map((n) => ({
      key: n.to,
      label: `Go to ${n.label}`,
      run: () => nav(n.to),
    }));
    const ask = t
      ? [{ key: 'ask', label: `Ask: “${text.trim()}”`, run: () => { nav('/console'); void send({ text: text.trim(), channel: 'typed', captured_at: new Date().toISOString(), speech_playing: false }); } }]
      : [];
    return [...ask, ...pages];
  }, [text, nav, send, allowed]);

  if (!open) {
    return (
      <button type="button" className="palette-hint" onClick={() => setOpen(true)} aria-label="Open command palette (Ctrl+K)">
        <kbd>Ctrl</kbd> <kbd>K</kbd>
      </button>
    );
  }
  return (
    <div className="palette-backdrop" role="presentation" onMouseDown={() => setOpen(false)}>
      <div className="palette" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={input}
          value={text}
          placeholder="Ask JARVIS or jump to a page…"
          aria-label="Command"
          onChange={(e) => {
            setText(e.target.value);
            setSel(0);
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') setSel((s) => Math.min(s + 1, items.length - 1));
            else if (e.key === 'ArrowUp') setSel((s) => Math.max(s - 1, 0));
            else if (e.key === 'Enter' && items[sel]) {
              items[sel].run();
              setOpen(false);
            }
          }}
        />
        <ul role="listbox">
          {items.map((it, i) => (
            <li key={it.key} role="option" aria-selected={i === sel}>
              <button
                type="button"
                className={i === sel ? 'sel' : ''}
                onMouseEnter={() => setSel(i)}
                onClick={() => {
                  it.run();
                  setOpen(false);
                }}
              >
                {it.label}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
