import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { MailCategory, MailItem, MailThread } from '../api/types';
import { PageHead } from '../components/PageHead';
import { Async, Badge, ErrorBox } from '../components/ui';
import { MAIL_CATEGORY, relTime } from '../components/Visuals';
import { useAsync } from '../lib/hooks';
import { useConversation } from '../state/app';

/* Triage lanes: what needs you, what you're waiting on, what's just for your information. Bulk mail stays folded. */
const LANES: { key: MailCategory; hint: string }[] = [
  { key: 'needs_reply', hint: 'Someone is waiting on you' },
  { key: 'waiting', hint: 'You are waiting on someone' },
  { key: 'fyi', hint: 'Worth knowing' },
];

function Thread({ item, onClose }: { item: MailItem; onClose: () => void }) {
  const t = useAsync<MailThread>(() => api.inboxThread(item.ref), [item.ref]);
  const { send } = useConversation();
  const nav = useNavigate();
  const ask = (text: string) => {
    nav('/console');
    void send({ text, channel: 'typed', captured_at: new Date().toISOString(), speech_playing: false });
  };
  return (
    <div className="thread" role="region" aria-label={`Thread: ${item.subject}`}>
      <div className="row gap wrap thread-actions">
        <button type="button" className="btn btn-primary btn-small" onClick={() => ask(`Draft a reply to the email “${item.subject}” from ${item.from_name} (ref ${item.ref}). Keep it short.`)}>
          Draft a reply
        </button>
        <button type="button" className="btn btn-small" onClick={() => ask(`Summarize the email thread “${item.subject}” (ref ${item.ref}) and tell me what they want from me.`)}>
          Summarize
        </button>
        {item.web_url && (
          <a className="btn btn-small btn-ghost" href={item.web_url} target="_blank" rel="noreferrer">
            Open in {item.mailbox === 'gmail' ? 'Gmail' : 'Outlook'}
          </a>
        )}
        <button type="button" className="btn btn-small btn-ghost" onClick={onClose}>
          Close
        </button>
      </div>
      <Async state={t}>
        {(th) => (
          <ol className="plain thread-msgs">
            {th.messages.map((m) => (
              <li key={m.ref}>
                <p className="small muted">
                  {m.from} · {m.date ? new Date(m.date).toLocaleString() : ''}
                </p>
                <pre className="email-body">{m.text}</pre>
              </li>
            ))}
          </ol>
        )}
      </Async>
    </div>
  );
}

function Row({ m, open, onOpen, children }: { m: MailItem; open: boolean; onOpen: () => void; children?: ReactNode }) {
  return (
    <li className={`mail-row ${m.unread ? 'unread' : ''} ${open ? 'open' : ''}`}>
      <button type="button" className="mail-btn" onClick={onOpen} aria-expanded={open}>
        <span className="who">{m.from_name}</span>
        <span className="subj">{m.subject}</span>
        <span className="snip">{m.snippet}</span>
        <span className="when mono">{relTime(m.date)}</span>
      </button>
      {children}
    </li>
  );
}

export function InboxPage() {
  const [unreadOnly, setUnreadOnly] = useState(true);
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const inbox = useAsync(() => api.inbox({ unread_only: unreadOnly && !query, limit: 40, q: query || undefined }), [unreadOnly, query]);
  return (
    <div className="page inbox-page">
      <PageHead title="Inbox" guide="connections">
        Gmail and Outlook in one place, sorted by what needs you. Replies are drafted for your review; nothing is sent without you.
      </PageHead>
      <form
        className="row gap wrap inbox-tools"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(q.trim());
        }}
      >
        <input aria-label="Search email" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your email" />
        <button type="submit" className="btn btn-small">
          Search
        </button>
        <label className="small inline-check">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} /> Unread only
        </label>
      </form>
      <Async state={inbox}>
        {(r) => {
          const boxes = Object.entries(r.mailboxes ?? {});
          const none = boxes.every(([, v]) => v.status !== 'connected');
          if (none)
            return (
              <div className="empty-call">
                <p>No mailbox is connected yet.</p>
                <a className="btn btn-primary" href="/connections#connect-email">
                  Connect Gmail or Outlook
                </a>
              </div>
            );
          const down = Object.entries(r.sources).filter(([, s]) => s.status === 'unavailable');
          return (
            <>
              {down.length > 0 && (
                <p className="warn-text small">
                  {down.map(([k]) => k).join(', ')} didn't respond. What's shown may be incomplete.
                </p>
              )}
              <div className="lanes">
                {LANES.map((l) => {
                  const items = r.items.filter((i) => i.category === l.key);
                  return (
                    <section key={l.key} className={`lane lane-${l.key}`} aria-label={MAIL_CATEGORY[l.key]}>
                      <h2>
                        {MAIL_CATEGORY[l.key]} <Badge tone={l.key === 'needs_reply' && items.length ? 'warn' : 'neutral'}>{items.length}</Badge>
                      </h2>
                      <p className="small muted">{l.hint}</p>
                      {items.length === 0 ? (
                        <p className="muted small">Clear.</p>
                      ) : (
                        <ul className="plain">
                          {items.map((m) => (
                            <Row key={m.ref} m={m} open={open === m.ref} onOpen={() => setOpen(open === m.ref ? null : m.ref)}>
                              {open === m.ref && <Thread item={m} onClose={() => setOpen(null)} />}
                            </Row>
                          ))}
                        </ul>
                      )}
                    </section>
                  );
                })}
              </div>
              {r.counts.bulk > 0 && <p className="small muted">{r.counts.bulk} newsletters and notifications folded away.</p>}
            </>
          );
        }}
      </Async>
      <ErrorBox error={inbox.error && inbox.data ? inbox.error : null} />
    </div>
  );
}
