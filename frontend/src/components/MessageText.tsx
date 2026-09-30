import { Fragment, type ReactNode } from 'react';

/** Renders Jarvis replies: "▸ Section" headers, bullet lines, and **bold** as real bold — never raw markdown symbols. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*|__)(.+?)\1|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[2] !== undefined ? <strong key={k++}>{m[2]}</strong> : <code key={k++}>{m[3]}</code>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  // stray single *emphasis* markers → plain text
  return out.map((p) => (typeof p === 'string' ? p.replace(/(^|\s)[*_](\S[^*_]*?\S|\S)[*_](?=\s|$|[.,!?])/g, '$1$2') : p));
}

export function MessageText({ text }: { text: string | null | undefined }) {
  const lines = (text ?? '').split('\n');
  return (
    <div className="bubble-text msg-rich">
      {lines.map((raw, i) => {
        const line = raw.replace(/^#{1,6}\s+/, '');
        if (/^\s*▸\s+/.test(line)) return <h4 key={i} className="msg-section">{line.replace(/^\s*▸\s+/, '')}</h4>;
        const bullet = line.match(/^\s*(?:[-*•+]|\d+[.)])\s+(.*)$/);
        if (bullet)
          return (
            <div key={i} className="msg-bullet">
              <span aria-hidden="true">◆</span>
              <span>{inline(bullet[1])}</span>
            </div>
          );
        if (!line.trim()) return <div key={i} className="msg-gap" />;
        return (
          <Fragment key={i}>
            <p className="msg-line">{inline(line)}</p>
          </Fragment>
        );
      })}
    </div>
  );
}
