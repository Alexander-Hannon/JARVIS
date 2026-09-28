import { api } from '../api/client';
import { Async, Badge, Card } from '../components/ui';
import { fmtDateTime } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';

export function ActivityPage() {
  const activity = useAsync(() => api.activity(150), []);
  useInterval(() => void activity.reload(), 15000);
  return (
    <div className="page">
      <header className="page-head">
        <h1>Activity</h1>
        <button type="button" className="btn btn-small" onClick={() => void activity.reload()}>
          Refresh
        </button>
      </header>
      <Card>
        <Async state={activity} empty="No activity recorded yet.">
          {(list) => (
            <ol className="activity">
              {list.map((a, i) => (
                <li key={`${a.at}-${i}`} className="activity-row">
                  <time dateTime={a.at} className="muted small">
                    {fmtDateTime(a.at)}
                  </time>
                  <Badge tone="neutral">{a.kind}</Badge>
                  <span>{a.message}</span>
                </li>
              ))}
            </ol>
          )}
        </Async>
      </Card>
    </div>
  );
}
