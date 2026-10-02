import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useAsync, useInterval } from '../lib/hooks';

/* "JARVIS noticed…": the proactive layer. Only real signals (approvals waiting, leave-by, conflicts, deadlines, email,
   mission findings); when there's nothing, it stays out of the way. */
export function HeadsUp() {
  const h = useAsync(() => api.headsup(), []);
  useInterval(() => void h.reload(), 120000);
  const cards = h.data?.cards ?? [];
  if (!cards.length) return null;
  return (
    <section className="headsup" aria-label="Heads-up">
      {cards.map((c, i) => (
        <Link key={`${c.kind}-${i}`} to={c.link} className={`hu hu-${c.tone}`}>
          <strong>{c.title}</strong>
          {c.detail && <span className="hu-detail">{c.detail}</span>}
          {c.items && c.items.length > 0 && (
            <ul className="hu-items">
              {c.items.map((it, k) => (
                <li key={k}>
                  <span className="hu-title">{it.title}</span>
                  {it.meta && <span className="hu-meta">{it.meta}</span>}
                  {it.when && <span className="hu-when">{it.when}</span>}
                </li>
              ))}
            </ul>
          )}
        </Link>
      ))}
    </section>
  );
}

export function WeeklyReport() {
  const r = useAsync(() => api.weeklyReport(), []);
  if (!r.data || (!r.data.commands && !r.data.actions_done && !r.data.mission_runs)) return null;
  const d = r.data;
  const hrs = d.minutes_saved_estimate >= 90 ? `${(d.minutes_saved_estimate / 60).toFixed(1)} hours` : `${d.minutes_saved_estimate} minutes`;
  return (
    <p className="weekly" title={`Estimate: ${d.estimate_basis}`}>
      This week JARVIS handled {d.commands} requests, completed {d.actions_done} changes ({d.auto_approved} by your rules) and ran{' '}
      {d.mission_runs} mission checks. Roughly {hrs} saved for ${d.spend_usd.toFixed(2)}.
    </p>
  );
}
