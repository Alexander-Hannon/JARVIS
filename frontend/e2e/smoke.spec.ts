// UI SMOKE TEST WITH MOCKED API: every route and settings section renders without
// uncaught errors given contract-shaped data. Not live integration evidence.
import { expect, test } from '@playwright/test';
import { MockApi, proposal, setupApp } from './mockApi';

function richApi(): MockApi {
  const now = new Date().toISOString();
  const api = new MockApi();
  const j = (json: unknown) => () => ({ json });
  api
    .on('GET', '/api/proposals', j([proposal()]))
    .on('GET', '/api/actions/history', j([{ id: 'a1', action_type: 'todoist.create_task', provider: 'todoist', account_label: 'aj', target: { id: 'p', name: 'Inbox' }, args: { title: 'Review lab report', due_date: '2026-09-29' }, state: 'verified', external_url: 'https://x.test/1', proposal_id: 'prop-0', proposal_title: 'Add task', approved_at: now, receipt: { verified_at: now, method: 'read_back', ok: true, simulated: true, observed: {} }, attempts: [{ at: now, state: 'verified' }], request_text: 'add review lab report tomorrow', undoable: true }]))
    .on('GET', '/api/projects', j([{ id: 'p1', slug: 'school', name: 'School / GTA', area: 'school', description: 'Grading and coursework', notion_url: 'https://notion.so/x', sensitive: true, next_actions: ['Grade quizzes', { title: 'Prep lab' }], memories_count: 4 }]))
    .on('GET', '/api/memories', j([{ id: 'm1', type: 'preference', status: 'active', content: 'Prefers morning deep work', version: 2, pinned: true, confirmed_by_user: true, project_id: null, entity: null, source_kind: 'conversation', source_ref: 'conv-1', observed_at: now, review_at: null, expires_at: null, superseded_by: null, conflict_with: null }]))
    .on('GET', '/api/documents', j([{ id: 'd1', title: 'Syllabus.pdf', mime: 'application/pdf', origin: 'upload', project_id: null, created_at: now, version: 1, sha256: 'abc', status: 'ready', processing: { extract: { status: 'done' }, visual: { images: 2, rendered_pages: 3, needs_vision: true } }, segments: 12 }]))
    .on('GET', '/api/briefings', j([{ id: 'b1', kind: 'daily_command', for_date: '2026-09-28', content: { headline: 'Grade first, then lab prep', mission: 'Finish grading', priorities: ['Grade quizzes'], agenda: [{ title: 'Lab', start: now }], due: [], deadlines: [], windows: [], conflicts: [], obstacle: 'Meetings', first_action: 'Open gradebook', sections: [{ title: 'Notes', items: ['x'] }] }, spoken_summary: 'Grade first.', sources: { todoist: { status: 'ok', last_sync: now, detail: null }, calendar: 'not_connected' }, generated_by: 'schedule', catch_up: true, created_at: now }]))
    .on('GET', '/api/plan/history', j([{ mission: 'Ship', priorities: ['a'], constraints: [], first_action: null, version: 2, author: 'user', created_at: now, date: '2026-09-28' }]))
    .on('GET', '/api/activity', j([{ at: now, kind: 'run', message: 'Reading selected calendars' }]))
    .on('GET', '/api/notifications', j([{ id: 'n1', kind: 'briefing', title: 'Daily Command ready', body: 'Grade first', ref_type: 'briefing', ref_id: 'b1', created_at: now, read_at: null, push_status: 'accepted', opened_at: null }]))
    .on('GET', '/api/integrations', j([{ provider: 'todoist', account_id: 'acc1', status: 'connected', label: 'aj', scopes: ['data:read_write'], last_sync_at: now, last_error: null, grants: [{ resource_type: 'project', resource_id: 'p1', resource_name: 'Inbox', access: 'write', is_default_target: true }], live_verified: false }]))
    .on('GET', '/api/integrations/todoist/projects', j([{ id: 'p1', name: 'Inbox' }]))
    .on('GET', '/api/integrations/microsoft/setup', j({ configured: false, redirect_uri: 'http://127.0.0.1:8765/api/integrations/microsoft/callback', tenant: 'common', scopes: ['Mail.ReadWrite'], send_scope_added_when_enabled: 'Mail.Send', steps: ['Register an app'] }))
    .on('GET', '/api/connectors', j([{ id: 'notion', name: 'Notion', family: 'notes', auth: 'token', summary: 'Notes', reads: ['Search pages'], writes: ['notion.page.create'], fields: [{ key: 'token', label: 'Secret', secret: true, placeholder: '' }], steps: ['Create integration'], status: 'not_connected', simulated: false, account: null, resource: null }]))
    .on('GET', '/api/mcp', j([]))
    .on('GET', '/api/vault', j({ indexed_roots: [], last_index: {} }))
    .on('GET', '/api/inbox', j({ items: [], counts: { needs_reply: 0, waiting: 0, fyi: 0, bulk: 0 }, sources: {}, mailboxes: { gmail: { status: 'not_connected', address: null }, outlook: { status: 'not_connected', address: null } } }))
    .on('GET', '/api/integrations/google/setup', j({ configured: false, redirect_uri: 'http://127.0.0.1:8765/api/integrations/google/callback', required_apis: ['Google Calendar API', 'Google Drive API'], scopes: { calendar: ['calendar.readonly'], drive: ['drive.readonly'] } }))
    .on('GET', '/api/github/status', j({ connected: false }))
    .on('GET', '/api/voice/status', j({ engine: 'faster-whisper', model: 'small', device: 'cpu', state: 'ready', downloaded: true, download_size_mb: 466, load_ms: 1200, warmup_ms: 900, error: null, local_only: true, hardware: { cpu_count: 8 }, last_latency_ms: 812 }))
    .on('GET', '/api/schedules', j([{ id: 's1', key: 'daily_command', workflow: 'daily_command', label: 'Daily Command', tz: 'America/Chicago', hour: 6, minute: 30, weekdays: null, enabled: false, destination: null, next_run_at: null, last_run_at: null, last_status: null, last_success_at: null }]))
    .on('GET', '/api/worker/status', j({ alive: false, last_beat_at: null, worker_id: null, pid: null }))
    .on('GET', '/api/push/config', j({ supported: false, vapid_public_key: null, reason: 'pywebpush not installed' }))
    .on('GET', '/api/budget', j({ paid_inference_enabled: true, background_paid_enabled: false, daily_limit_usd: 2, monthly_limit_usd: 30, max_output_tokens: 2000, max_tool_calls: 8, max_specialists: 2, effort: 'medium', spent_today_usd: 0.42, spent_month_usd: 5.1, reserved_usd: 0.05, pricing: { 'claude-x': { input_per_mtok: 3 } }, measured_vs_estimated: { ratio: 0.94 } }))
    .on('GET', '/api/localops/roots', j([{ id: 'r1', path: '/home/aj/jarvis-workspace', label: 'Workspace', writable: false }]))
    .on('GET', '/api/backups', j([{ id: 'bk1', path: '/data/backups/bk1.tar', sha256: 'deadbeef', bytes: 204800, schema_version: 3, created_at: now }]))
    .on('GET', '/api/doctor', j({ checks: [{ name: 'database', status: 'ok', detail: 'schema v3', remedy: null }, { name: 'stt', status: 'warn', detail: 'model not warmed', remedy: 'Settings → Voice → Warm up' }] }))
    .on('GET', '/api/metrics/latency', j({ stt: { count: 3, p50_ms: 800, p95_ms: 1400, last_ms: 812 } }));
  return api;
}

const ROUTES = ['/', '/console', '/actions', '/projects', '/memory', '/briefings', '/activity', '/notifications', '/more'];
const SECTIONS = ['integrations', 'voice', 'schedules', 'notifications', 'budget', 'privacy', 'local', 'backups', 'doctor', 'security'];

test('smoke: every route and settings section renders (mocked API)', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await setupApp(page, richApi());
  for (const r of ROUTES) {
    await page.goto(r);
    await expect(page.locator('h1').first()).toBeVisible();
  }
  await page.goto('/actions');
  await page.getByRole('tab', { name: 'History' }).click();
  await expect(page.getByTestId('history-entry')).toContainText('add review lab report tomorrow');
  await expect(page.getByTestId('history-entry')).toContainText('SIMULATED / contract mock');
  await page.goto('/memory');
  await page.getByRole('tab', { name: 'Documents' }).click();
  await expect(page.getByText('needs vision')).toBeVisible();
  for (const s of SECTIONS) {
    await page.goto(`/settings#${s}`);
    await expect(page.locator('.settings-body .card').first()).toBeVisible();
  }
  await page.goto('/settings#schedules');
  await expect(page.getByText('laptop sleeps')).toBeVisible();
  await expect(page.getByText('6:30 AM Daily Command')).toBeVisible();
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto('/settings#voice');
  await page.screenshot({ path: 'e2e/screenshots/desktop-settings-voice.png' });
  expect(errors).toEqual([]);
});
