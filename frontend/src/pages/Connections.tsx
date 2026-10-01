import { useLocation } from 'react-router-dom';
import { PageHead } from '../components/PageHead';
import { IntegrationsSection } from './settings/Integrations';

const ERRORS: Record<string, string> = {
  wrong_browser: 'Finish the connection in the same browser (and address) where you clicked Connect.',
  bad_state: 'That sign-in link expired or was already used. Start the connection again.',
  denied: 'The provider reported that access was not granted.',
  token_exchange_failed: 'The provider rejected the sign-in. Check the app registration and try again.',
};

export function ConnectionsPage() {
  const params = new URLSearchParams(useLocation().search);
  const ok = params.get('connected');
  const err = params.get('connect_error');
  return (
    <div className="page connections-page">
      <PageHead title="Connections" guide="connections">
        The apps JARVIS can use. Each one only gets what you grant here.
      </PageHead>
      {ok && <p className="notice notice-ok" role="status">Connected {ok}. Finish any choices below (calendars, pages, devices).</p>}
      {err && <p className="notice notice-warn" role="alert">Connection failed: {ERRORS[err] ?? err}</p>}
      <IntegrationsSection />
    </div>
  );
}
