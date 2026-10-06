import { useState } from 'react';
import { api } from '../api/client';
import { useAsync } from '../lib/hooks';
import { Async, Badge, Card, ErrorBox, SecretForm, Toggle } from './ui';
import { useAuth } from '../state/auth';

/* Talk to JARVIS from Telegram. The bot answers only the one chat you pair with a code from here. */
export function TelegramCard() {
  const { can } = useAuth();
  const st = useAsync(() => api.telegram(), []);
  const [pair, setPair] = useState<{ code: string; instructions: string } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      void st.reload();
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <Card title="Telegram" id="connect-telegram">
      <p className="muted small">
        Message JARVIS from your phone, approve with a button, and get mission results there. The bot only answers the chat you
        pair, and forwarded messages are saved as notes rather than followed as instructions.
      </p>
      <Async state={st}>
        {(s) =>
          s.status === 'not_connected' ? (
            <>
              <ol className="small">
                <li>In Telegram, message @BotFather → /newbot → copy the token.</li>
                <li>Paste it here, then pair your chat with a one-time code.</li>
              </ol>
              {can('org.integrations') ? (
                <SecretForm label="Bot token" placeholder="123456:ABC…" submitLabel="Connect bot" onSubmit={(v) => run(() => api.connectTelegram(v))} />
              ) : (
                <p className="hint">Your admin connects the organization's bot; then you can pair your own chat here.</p>
              )}
            </>
          ) : (
            <div className="stack">
              <div>
                <strong>@{s.bot}</strong>{' '}
                <Badge tone={s.status === 'paired' ? 'ok' : 'warn'}>{s.status === 'paired' ? `paired with ${s.chat_name}` : 'not paired'}</Badge>
                {s.simulated && <Badge tone="info">simulated</Badge>}
              </div>
              {s.status !== 'paired' ? (
                <div className="row gap wrap">
                  <button type="button" className="btn btn-primary btn-small" onClick={() => void run(async () => setPair(await api.pairTelegram()))}>
                    Get pairing code
                  </button>
                  {pair && (
                    <span>
                      Send <code className="mono">/pair {pair.code}</code> to @{s.bot} within 10 minutes.
                    </span>
                  )}
                </div>
              ) : (
                <>
                  <Toggle
                    label="Send mission results, approvals and briefings to Telegram"
                    checked={s.notify ?? true}
                    onChange={(v) => void run(() => api.telegramNotify(v))}
                  />
                  <button type="button" className="btn btn-small btn-danger-ghost" onClick={() => void run(() => api.unpairTelegram())}>
                    Unpair chat
                  </button>
                </>
              )}
            </div>
          )
        }
      </Async>
      <ErrorBox error={err} />
    </Card>
  );
}
