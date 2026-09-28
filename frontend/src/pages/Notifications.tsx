import { useEffect } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { Badge, Card, Empty, ErrorBox } from '../components/ui';
import { fmtDateTime } from '../lib/format';
import { useNotifications } from '../state/app';

const PUSH_LABEL: Record<string, string> = {
  generated: 'Generated (in-app only)',
  queued: 'Queued for push',
  accepted: 'Accepted by push service (not proof you saw it)',
  failed: 'Push failed',
};

export function NotificationsPage() {
  const n = useNotifications();
  const [params] = useSearchParams();
  const opened = params.get('opened');
  // Arrived by tapping a push notification: record that it was actually opened.
  useEffect(() => {
    if (opened) void api.markNotificationOpened(opened).then(() => n.refresh()).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);
  return (
    <div className="page">
      <header className="page-head">
        <h1>Notifications</h1>
        <Link to="/settings#notifications" className="btn btn-small">
          Notification settings
        </Link>
      </header>
      <ErrorBox error={n.error} onRetry={() => void n.refresh()} />
      <Card>
        {n.items.length === 0 ? (
          <Empty>No notifications.</Empty>
        ) : (
          <ul className="list">
            {n.items.map((x) => (
              <li key={x.id} className={`list-row notif ${x.read_at ? 'read' : 'unread'}`}>
                <div className="grow">
                  <strong>{x.title}</strong>
                  <p>{x.body}</p>
                  <span className="muted small">
                    {fmtDateTime(x.created_at)} · {x.push_status ? PUSH_LABEL[x.push_status] ?? x.push_status : 'in-app'}
                    {x.opened_at ? ` · opened ${fmtDateTime(x.opened_at)}` : ''}
                  </span>
                </div>
                {!x.read_at ? (
                  <button
                    type="button"
                    className="btn btn-small"
                    onClick={() => void n.markRead(x.id)}
                  >
                    Mark read
                  </button>
                ) : (
                  <Badge tone="neutral">read</Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
