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
    .on('GET', '/api/channels/telegram', j({ status: 'not_connected' }))
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
    .on('GET', '/api/headsup', j({ cards: [{ kind: 'approvals', tone: 'attention', title: '1 waiting for your approval', detail: 'Add task', link: '/actions' }, { kind: 'leave_by', tone: 'attention', title: 'Leave by 4:02 PM for Advisor meeting', detail: 'About 20 min', link: '/' }] }))
    .on('GET', '/api/report/weekly', j({ commands: 12, actions_done: 5, auto_approved: 2, mission_runs: 9, spend_usd: 0.84, minutes_saved_estimate: 49, estimate_basis: 'x' }))
    .on('GET', '/api/missions', j({ missions: [{ id: 'm1', name: 'Inbox watch', goal: 'Check my inbox for anything from my advisor.', schedule: { kind: 'interval', minutes: 120 }, tz: 'America/Chicago', families: ['email'], allow_proposals: false, output: 'quiet', max_cost_usd: 0.25, monthly_cost_usd: 5, month_spend: 0.4, quiet_start: '22:00', quiet_end: '07:00', enabled: true, next_run_at: new Date(Date.now() + 3600e3).toISOString(), last_run_at: now, last_status: 'done', last_summary: 'Dr. Lee wants grades by Friday.', runs: [{ id: 'r1', run_id: 'x', started_at: now, finished_at: now, status: 'done', summary: 'Dr. Lee wants grades by Friday.', cost_usd: 0.03, proposal_id: null }] }], templates: [{ key: 'inbox_watch', name: 'Inbox watch', goal: 'x', schedule: { kind: 'interval', minutes: 120 }, families: ['email'], output: 'quiet' }], families: ['chief', 'email', 'calendar'], read_only_families: ['chief'] }))
    .on('GET', '/api/briefings', j([{ id: 'b1', kind: 'daily_command', for_date: '2026-10-01', content: { headline: 'Grading day with a GTA shift at six', mission: 'Finish IS 640 grading', first_action: 'Open the rubric' }, spoken_summary: 'Grading day.', sources: null, generated_by: 'deterministic', catch_up: false, created_at: now }]))
    .on('GET', '/api/plan/history', j([]))
    .on('GET', '/api/inbox', j({ items: [{ ref: 'gmail:1', mailbox: 'gmail', category: 'needs_reply', from_name: 'Dr. Lee', sender_email: 'lee@uah.edu', subject: 'Grades', snippet: 'Can you send the midterm grades?', date: now, unread: true, web_url: null }, { ref: 'gmail:2', mailbox: 'gmail', category: 'fyi', from_name: 'Sam', sender_email: 's@x', subject: 'Lab moved', snippet: 'Room 210', date: now, unread: false, web_url: null }], counts: { needs_reply: 1, waiting: 0, fyi: 1, bulk: 3 }, sources: { gmail: { status: 'ok', count: 2 } }, mailboxes: { gmail: { status: 'connected', address: 'aj@example.test' }, outlook: { status: 'not_connected', address: null } } }))
    .on('GET', '/api/sandbox', j({ runtime: 'docker', available: false, detail: 'Docker isn’t available in WSL. Enable Docker Desktop → Settings → Resources → WSL integration.', simulated: false, network: { active: false, domains: [], until: null }, latest_shot: null, computer_use: true }))
    .on('GET', '/api/sandbox/history', j([{ id: 'x1', run_id: null, kind: 'exec', detail: 'python3 analyze.py', exit_code: 0, output: 'Total: $1,204', duration_ms: 230, created_at: now }]))
    .on('GET', '/api/skills', j([{ name: 'weekly-report', title: 'weekly-report', description: 'How AJ formats his weekly GTA report', enabled: true, files: ['SKILL.md'], chars: 120 }]))
    .on('GET', '/api/manual', j({ assistant_name: 'JARVIS', guides: [{ id: 'getting-started', title: 'Getting started', markdown: '# Getting started\nTalk to it.\n\n- One\n- Two' }], reference: { capabilities: [{ family: 'email', label: 'Email', tools: [{ name: 'check_inbox', what: 'Triage email', example: 'Anything to answer?', risk: 'Reads only' }] }], actions: [{ action_type: 'task.create', label: 'Create a Todoist task', provider: 'todoist', tier: 1, automatable: true, undo: 'task.delete', reaches_others: false }, { action_type: 'gmail.send', label: 'Send an email from Gmail', provider: 'gmail', tier: 2, automatable: false, undo: null, reaches_others: true }], connectors: [] } }))
    .on('GET', '/api/policies', j({ rules: [], catalog: [{ action_type: 'task.create', label: 'Create a Todoist task', provider: 'todoist', tier: 1, automatable: true, undo: 'task.delete', reaches_others: false }], undo_window_minutes: 5 }))
    .on('GET', '/api/metrics/latency', j({ stt: { count: 3, p50_ms: 800, p95_ms: 1400, last_ms: 812 } }));
  return api;
}

const ROUTES = ['/', '/console', '/inbox', '/actions', '/missions', '/workbench', '/connections', '/help', '/projects', '/memory', '/briefings', '/activity', '/notifications', '/more'];
const SECTIONS = ['integrations', 'voice', 'schedules', 'notifications', 'budget', 'privacy', 'local', 'backups', 'doctor', 'security'];

test('smoke: every route and settings section renders (mocked API)', async ({ page }) => {
  test.setTimeout(120_000); // also captures desktop + phone screenshots of every V4 page
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
  await page.getByRole('tab', { name: 'Rules' }).click();
  await expect(page.getByText('No rules yet')).toBeVisible();
  await page.goto('/');
  await expect(page.getByText('Leave by 4:02 PM for Advisor meeting')).toBeVisible();
  await page.goto('/missions');
  await expect(page.getByText('Dr. Lee wants grades by Friday.').first()).toBeVisible();
  await page.goto('/inbox');
  await expect(page.getByText('Can you send the midterm grades?')).toBeVisible();
  await page.goto('/workbench');
  await expect(page.getByRole('heading', { name: 'Set up the sandbox' })).toBeVisible();
  await page.goto('/help');
  await expect(page.getByText('Send an email from Gmail')).toBeVisible();
  for (const r of ['/', '/missions', '/inbox', '/workbench', '/help']) {
    for (const [w, h, tag] of [[1366, 900, 'desktop'], [390, 844, 'phone']] as const) {
      await page.setViewportSize({ width: w, height: h });
      await page.goto(r);
      await page.waitForTimeout(250);
      await page.screenshot({ path: `e2e/screenshots/v4-${tag}${r === '/' ? '-today' : r.replace('/', '-')}.png`, fullPage: true });
    }
  }
  await page.setViewportSize({ width: 1280, height: 720 });
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
