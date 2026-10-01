import { Fragment, type ReactNode } from 'react';

/* Minimal Markdown for the bundled Help guides (headings, paragraphs, lists, tables, **bold**, `code`).
   Renders React elements only — never raw HTML. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g).map((p, i) =>
    p.startsWith('**') ? <strong key={i}>{p.slice(2, -2)}</strong> : p.startsWith('`') ? <code key={i}>{p.slice(1, -1)}</code> : p.startsWith('*') && p.length > 2 ? <em key={i}>{p.slice(1, -1)}</em> : <Fragment key={i}>{p}</Fragment>,
  );
}

export function Md({ text, skipTitle = false }: { text: string; skipTitle?: boolean }) {
  const lines = text.split('\n');
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) {
      i++;
      continue;
    }
    if (l.startsWith('# ')) {
      if (!skipTitle) out.push(<h2 key={i}>{inline(l.slice(2))}</h2>);
      i++;
    } else if (l.startsWith('## ')) {
      out.push(<h3 key={i}>{inline(l.slice(3))}</h3>);
      i++;
    } else if (l.startsWith('|')) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith('|')) {
        if (!/^\|[-| ]+\|$/.test(lines[i].trim())) rows.push(lines[i].split('|').slice(1, -1).map((c) => c.trim()));
        i++;
      }
      out.push(
        <table key={i} className="md-table">
          <thead>
            <tr>{rows[0].map((c, j) => <th key={j} scope="col">{inline(c)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.slice(1).map((r, k) => (
              <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
    } else if (/^(- |\d+\. )/.test(l)) {
      const ordered = /^\d+\. /.test(l);
      const items: string[] = [];
      while (i < lines.length && /^(- |\d+\. )/.test(lines[i])) {
        let item = lines[i].replace(/^(- |\d+\. )/, '');
        i++;
        while (i < lines.length && /^ {2,}\S/.test(lines[i])) item += ' ' + lines[i++].trim();
        items.push(item);
      }
      const List = ordered ? 'ol' : 'ul';
      out.push(<List key={i}>{items.map((it, k) => <li key={k}>{inline(it)}</li>)}</List>);
    } else {
      let para = l;
      i++;
      while (i < lines.length && lines[i].trim() && !/^(#|\||- |\d+\. )/.test(lines[i])) para += ' ' + lines[i++].trim();
      out.push(<p key={i}>{inline(para)}</p>);
    }
  }
  return <div className="md">{out}</div>;
}
