// MOCKED API for UI tests. Nothing here talks to a real JARVIS backend.
import type { Page, Request, Route } from '@playwright/test';
import type { Capture, CommandResponse, ControlState, Proposal, ProposedItem, Today } from '../src/api/types';

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  json: unknown;
  raw: Buffer | null;
}

export const CSRF = 'csrf-test-token';

export function item(index: number, over: Partial<ProposedItem> = {}): ProposedItem {
  return {
    index,
    action_type: 'todoist.create_task',
    provider: 'todoist',
    account_label: 'aj@example.test',
    target: { id: 'proj-1', name: 'Inbox' },
    args: { title: `Task ${index + 1}`, due_date: '2026-09-29', timezone: 'America/Chicago', duration_minutes: 45 },
    before: null,
    after: null,
    expected_effect: 'Creates one task',
    reason: 'You asked for it',
    missing_fields: [],
    state: 'proposed',
    external_id: null,
    external_url: null,
    last_error: null,
    receipt: null,
    ...over,
  };
}

export function proposal(over: Partial<Proposal> = {}): Proposal {
  return {
    id: 'prop-1',
    status: 'proposed',
    title: 'Add 2 tasks to Todoist',
    version: 1,
    hash: 'hash-v1',
    summary: 'Create tasks for tomorrow.',
    assumptions: ['"Tomorrow" means Tue Sep 29 (America/Chicago)'],
    questions: [],
    expires_at: new Date(Date.now() + 3600_000).toISOString(),
    presented_at: new Date().toISOString(),
    items: [
      item(0, { args: { title: 'Review lab report', due_date: '2026-09-29', timezone: 'America/Chicago', duration_minutes: 45 } }),
      item(1, { args: { title: 'Email advisor', due_date: null }, missing_fields: ['due_date'] }),
    ],
    versions: [{ version: 1, hash: 'hash-v1', created_at: new Date().toISOString(), superseded_at: null }],
    approval: null,
    ...over,
  };
}

export function verified(p: Proposal, indexes: number[]): Proposal {
  return {
    ...p,
    status: 'verified',
    items: p.items.map((it) =>
      indexes.includes(it.index)
        ? {
            ...it,
            state: 'verified',
            external_id: `ext-${it.index}`,
            external_url: `https://todoist.example.test/task/${it.index}`,
            receipt: { verified_at: new Date().toISOString(), method: 'read_back', ok: true, simulated: true, observed: {} },
          }
        : { ...it, state: 'skipped' },
    ),
  };
}

export function today(): Today {
  return {
    date: '2026-09-28',
    tz: 'America/Chicago',
    now: new Date().toISOString(),
    plan: { mission: 'Ship the JARVIS frontend', priorities: ['Voice capture', 'Approvals', 'Mobile layout'], constraints: [], first_action: 'Open the console', version: 3, author: 'user' },
    agenda: [{ id: 'e1', title: 'Lab section', start: '2026-09-28T15:00:00Z', end: '2026-09-28T16:30:00Z', calendar: 'School' }],
    tasks_due: [{ id: 't1', content: 'Grade quizzes', due: { date: '2026-09-28' } }],
    deadlines: [],
    conflicts: [],
    work_windows: [{ start: '2026-09-28T17:00:00Z', end: '2026-09-28T19:00:00Z', label: 'Deep work' }],
    sources: {
      todoist: { status: 'ok', last_sync: new Date().toISOString(), detail: null },
      calendar: { status: 'stale', last_sync: new Date(Date.now() - 7200_000).toISOString(), detail: 'Token refresh pending' },
      deadlines: { status: 'not_connected', last_sync: null, detail: 'No ICS feed' },
    },
  };
}

export function commandResponse(p: Proposal | null, text = 'I drafted 2 tasks. Approve to add them.'): CommandResponse {
  return {
    run_id: 'run-1',
    status: 'awaiting_approval',
    message: { id: 'm-2', role: 'assistant', text, spoken_text: 'Two tasks drafted. Approve to add them.', capture_id: null, proposal_id: p?.id ?? null, created_at: new Date().toISOString() },
    proposal: p,
    questions: [],
    citations: [],
    events: [
      { id: 'ev1', run_id: 'run-1', kind: 'route', message: 'Routing to Tasks specialist', data: null, created_at: new Date().toISOString() },
      { id: 'ev2', run_id: 'run-1', kind: 'await', message: 'Awaiting your approval', data: null, created_at: new Date().toISOString() },
    ],
    intent: 'capture',
    specialists: ['tasks'],
  };
}

export function capture(over: Partial<Capture> = {}): Capture {
  return {
    capture_id: 'cap-1',
    status: 'transcribed',
    audio: { mime: 'audio/webm', bytes: 12345, duration_s: 1.4, format: 'webm' },
    transcript: { version: 1, text: 'add review lab report tomorrow', source: 'stt', confidence: 0.91, latency_ms: 812, model: 'whisper-small' },
    error: null,
    ...over,
  };
}

type Handler = (req: Request, call: Call) => Promise<{ status?: number; json?: unknown } | undefined> | { status?: number; json?: unknown } | undefined;

export class MockApi {
  calls: Call[] = [];
  authenticated = true;
  control: ControlState = { actions_paused: false, paused_at: null, reason: null, in_flight: [] };
  private handlers: { method: string; path: string | RegExp; fn: Handler }[] = [];

  on(method: string, path: string | RegExp, fn: Handler): this {
    this.handlers.unshift({ method, path, fn });
    return this;
  }

  callsTo(method: string, path: string | RegExp): Call[] {
    return this.calls.filter((c) => c.method === method && (typeof path === 'string' ? c.path === path : path.test(c.path)));
  }

  async install(page: Page): Promise<void> {
    await page.route(
      (url) => url.pathname.startsWith('/api/'),
      async (route: Route) => {
        const req = route.request();
        const url = new URL(req.url());
        const raw = req.postDataBuffer();
        let json: unknown = null;
        const ct = req.headers()['content-type'] ?? '';
        if (raw && ct.includes('application/json')) {
          try {
            json = JSON.parse(raw.toString('utf8'));
          } catch {
            json = null;
          }
        }
        const call: Call = { method: req.method(), path: url.pathname + url.search, headers: req.headers(), json, raw };
        this.calls.push(call);
        const res = await this.dispatch(req, call, url.pathname);
        await route.fulfill({ status: res.status ?? 200, contentType: 'application/json', body: JSON.stringify(res.json ?? null) });
      },
    );
  }

  private async dispatch(req: Request, call: Call, pathname: string): Promise<{ status?: number; json?: unknown }> {
    for (const h of this.handlers) {
      if (h.method !== call.method) continue;
      const match = typeof h.path === 'string' ? h.path === pathname : h.path.test(pathname);
      if (!match) continue;
      const r = await h.fn(req, call);
      if (r) return r;
    }
    return this.defaults(call, pathname);
  }

  private defaults(call: Call, p: string): { status?: number; json?: unknown } {
    const m = call.method;
    if (p === '/api/auth/status') return { json: { owner_exists: true, authenticated: this.authenticated, csrf_token: this.authenticated ? CSRF : null, remote_mode: false, setup_allowed: false } };
    if (!this.authenticated && !p.startsWith('/api/auth/')) return { status: 401, json: { detail: 'Not authenticated' } };
    if (m !== 'GET' && call.headers['x-csrf-token'] !== CSRF) return { status: 403, json: { detail: { code: 'csrf', message: 'CSRF token missing' } } };
    if (p === '/api/control' && m === 'GET') return { json: this.control };
    if (p === '/api/control/pause') {
      this.control = { actions_paused: true, paused_at: new Date().toISOString(), reason: 'Paused from header', in_flight: [] };
      return { json: this.control };
    }
    if (p === '/api/control/resume') {
      this.control = { actions_paused: false, paused_at: null, reason: null, in_flight: [] };
      return { json: this.control };
    }
    if (p === '/api/notifications') return { json: [] };
    if (p === '/api/today') return { json: today() };
    if (p === '/api/settings') return { json: { voice: { voice_name: null, rate: 1, muted: false, auto_speak: true }, privacy: { retain_audio: false } } };
    if (p === '/api/conversations' && m === 'POST') return { json: { id: 'conv-1' } };
    if (/^\/api\/conversations\/[^/]+\/messages$/.test(p)) return { json: [] };
    if (p === '/api/proposals') return { json: [] };
    if (p === '/api/activity') return { json: [] };
    if (p === '/api/projects') return { json: [] };
    if (m === 'GET') return { json: [] };
    return { status: 404, json: { detail: `mock: no handler for ${m} ${p}` } };
  }
}

export async function setupApp(page: Page, api = new MockApi()): Promise<MockApi> {
  // Speech synthesis is muted in tests so no audio device is required.
  await page.addInitScript(() => {
    try {
      localStorage.setItem('jarvis.voicePrefs', JSON.stringify({ voiceName: null, rate: 1, muted: true }));
    } catch {
      /* ignore */
    }
  });
  await api.install(page);
  return api;
}
