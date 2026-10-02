// UI TESTS WITH MOCKED API (page.route). These verify frontend behaviour only;
// they are NOT live integration evidence for the backend, STT, or providers.
import { expect, test, type Page } from '@playwright/test';
import { capture, commandResponse, CSRF, MockApi, proposal, setupApp, verified } from './mockApi';

const SHOTS = 'e2e/screenshots';

async function sendTyped(page: Page, text: string) {
  if (!(await page.getByTestId('cmd-input').isVisible())) await page.getByTestId('type-toggle').click(); // voice-first default
  await page.getByTestId('cmd-input').fill(text);
  await page.getByTestId('send-btn').click();
}

/** Wires a proposal lifecycle: command → proposal, approve → queued, poll → verified. */
function wireProposalFlow(api: MockApi, p = proposal()) {
  let current = p;
  let approvedIdx: number[] = [];
  api.on('POST', '/api/commands', () => ({ json: commandResponse(current) }));
  api.on('POST', `/api/proposals/${p.id}/approve`, (_r, call) => {
    const body = call.json as { version: number; hash: string; selected_items: number[] };
    if (body.version !== current.version || body.hash !== current.hash) return { status: 409, json: { detail: { code: 'stale', message: 'stale' } } };
    approvedIdx = body.selected_items;
    current = {
      ...current,
      status: 'queued',
      approval: { id: 'ap-1', selected_items: approvedIdx, channel: 'button', created_at: new Date().toISOString() },
      items: current.items.map((it) => (approvedIdx.includes(it.index) ? { ...it, state: 'queued' } : it)),
    };
    return { json: { approval_id: 'ap-1', proposal: current } };
  });
  api.on('GET', `/api/proposals/${p.id}`, () => {
    if (current.status === 'queued') current = verified(current, approvedIdx);
    return { json: current };
  });
  return {
    get current() {
      return current;
    },
    set current(v) {
      current = v;
    },
  };
}

test.describe('UI (mocked API)', () => {
  test('login gate: shows login, rejects bad password, then enters app and sends CSRF', async ({ page }) => {
    const api = new MockApi();
    api.authenticated = false;
    api.on('POST', '/api/auth/login', (_r, call) => {
      const pw = (call.json as { password: string }).password;
      if (pw !== 'correct horse battery') return { status: 401, json: { detail: 'Invalid password' } };
      api.authenticated = true;
      return { json: { csrf_token: CSRF } };
    });
    await setupApp(page, api);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Pair with a code instead' })).toBeVisible();
    await page.getByTestId('password').fill('wrong');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('alert')).toContainText('Invalid password');
    await page.getByTestId('password').fill('correct horse battery');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByTestId('mission')).toHaveText('Ship the JARVIS frontend');
    // Source status: disconnected source is shown explicitly, never "all clear".
    await expect(page.getByTestId('source-deadlines')).toContainText('Not connected');
    await expect(page.getByTestId('source-calendar')).toContainText('Stale');
    // Non-GET requests carry the in-memory CSRF token.
    await page.getByTestId('pause-toggle').click();
    await expect.poll(() => api.callsTo('POST', '/api/control/pause').length).toBe(1);
    expect(api.callsTo('POST', '/api/control/pause')[0].headers['x-csrf-token']).toBe(CSRF);
  });

  test('typed command → proposal card → select → approve → verified receipt', async ({ page }) => {
    const api = await setupApp(page);
    wireProposalFlow(api);
    await page.goto('/console');
    await sendTyped(page, 'add review lab report tomorrow and email advisor');
    const card = page.getByTestId('proposal-card');
    await expect(card).toBeVisible();
    await expect(page.getByText('Routing to Tasks specialist')).toBeVisible();
    // Item with missing fields cannot be selected; approve is disabled with a reason.
    await expect(card.getByTestId('item-1').getByRole('checkbox')).toBeDisabled();
    await expect(card.getByText('Needs Due date')).toBeVisible();
    await expect(card.getByTestId('approve-btn')).toBeDisabled();
    await expect(card.getByText('Select at least one ready item')).toBeVisible();
    await card.getByTestId('item-0').getByRole('checkbox').check();
    await card.getByTestId('approve-btn').click();
    await expect.poll(() => api.callsTo('POST', '/api/proposals/prop-1/approve').length).toBe(1);
    expect(api.callsTo('POST', '/api/proposals/prop-1/approve')[0].json).toEqual({ version: 1, hash: 'hash-v1', selected_items: [0] });
    const receipt = card.getByTestId('receipt-0');
    await expect(receipt).toContainText('Verified');
    await expect(receipt).toContainText('SIMULATED / contract mock');
    await expect(receipt.getByRole('link', { name: /Open in todoist/ })).toHaveAttribute('href', 'https://todoist.example.test/task/0');
    await page.screenshot({ path: `${SHOTS}/desktop-console-receipt.png`, fullPage: false });
  });

  test('editing an item calls revise and approval binds to the new version', async ({ page }) => {
    const api = await setupApp(page);
    const flow = wireProposalFlow(api);
    api.on('POST', '/api/proposals/prop-1/revise', (_r, call) => {
      const body = call.json as { items: { index: number; args: Record<string, unknown> }[] };
      const edit = body.items[0];
      flow.current = {
        ...flow.current,
        version: 2,
        hash: 'hash-v2',
        approval: null,
        items: flow.current.items.map((it) => (it.index === edit.index ? { ...it, args: edit.args, missing_fields: [] } : it)),
      };
      return { json: flow.current };
    });
    await page.goto('/console');
    await sendTyped(page, 'add two tasks');
    const card = page.getByTestId('proposal-card');
    const item1 = card.getByTestId('item-1');
    await item1.getByRole('button', { name: 'Edit' }).click();
    await item1.getByLabel(/Due date/).fill('2026-10-01');
    await item1.getByRole('button', { name: 'Save as new version' }).click();
    await expect(card.getByTestId('proposal-version')).toHaveText('v2');
    await expect(card.getByTestId('proposal-notice')).toContainText('version 2');
    const revise = api.callsTo('POST', '/api/proposals/prop-1/revise')[0];
    expect(revise.json).toMatchObject({ items: [{ index: 1, args: { title: 'Email advisor', due_date: '2026-10-01' } }] });
    // The formerly-missing item is now selectable; approval uses v2 + new hash.
    await item1.getByRole('checkbox').check();
    await card.getByTestId('approve-btn').click();
    await expect(card.getByTestId('receipt-1')).toContainText('Verified');
    expect(api.callsTo('POST', '/api/proposals/prop-1/approve')[0].json).toEqual({ version: 2, hash: 'hash-v2', selected_items: [1] });
  });

  test('409 stale approval refetches, explains, and re-approval uses the fresh version', async ({ page }) => {
    const api = await setupApp(page);
    const flow = wireProposalFlow(api);
    await page.goto('/console');
    await sendTyped(page, 'add two tasks');
    const card = page.getByTestId('proposal-card');
    await expect(card.getByTestId('proposal-version')).toHaveText('v1');
    // Server-side the plan changes to v3 before the user approves.
    flow.current = { ...flow.current, version: 3, hash: 'hash-v3' };
    let first = true;
    api.on('GET', '/api/proposals/prop-1', () => {
      if (first) {
        first = false;
        return { json: flow.current };
      }
      return undefined;
    });
    await card.getByTestId('item-0').getByRole('checkbox').check();
    await card.getByTestId('approve-btn').click();
    await expect(card.getByTestId('proposal-notice')).toContainText('changed since you reviewed it (now version 3)');
    await expect(card.getByTestId('proposal-notice')).toContainText('Nothing was executed');
    await expect(card.getByTestId('proposal-version')).toHaveText('v3');
    await expect(card.getByTestId('item-0').getByRole('checkbox')).not.toBeChecked();
    await card.getByTestId('item-0').getByRole('checkbox').check();
    await card.getByTestId('approve-btn').click();
    await expect(card.getByTestId('receipt-0')).toContainText('Verified');
    const approvals = api.callsTo('POST', '/api/proposals/prop-1/approve');
    expect(approvals.map((c) => (c.json as { version: number }).version)).toEqual([1, 3]);
  });

  test('microphone permission denied shows a voice error and focuses typed input', async ({ page }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
    });
    const api = await setupApp(page);
    await page.goto('/console');
    await page.getByTestId('record-btn').click();
    const err = page.getByTestId('voice-error');
    await expect(err).toContainText('Microphone unavailable');
    await expect(err).toContainText('permission was denied');
    await expect(err).toContainText('not a reasoning error');
    await expect(page.getByTestId('cmd-input')).toBeFocused();
    expect(api.callsTo('POST', '/api/captures/audio')).toHaveLength(0);
  });

  test('fake-media recording uploads multipart and shows an editable transcript sent as voice', async ({ page }) => {
    const api = await setupApp(page);
    api.on('POST', '/api/captures/audio', () => ({ json: capture() }));
    api.on('POST', '/api/captures/cap-1/transcript', (_r, call) => ({
      json: capture({ transcript: { version: 2, text: (call.json as { text: string }).text, source: 'user_edit', confidence: null, latency_ms: null, model: null } }),
    }));
    api.on('POST', '/api/commands', () => ({ json: commandResponse(null, 'Noted. Anything else?') }));
    // Review mode (auto-send off): the transcript is shown for editing before sending.
    await page.addInitScript(() => localStorage.setItem('jarvis.autoSend', 'false'));
    await page.goto('/console');
    const rec = page.getByTestId('record-btn');
    await rec.click(); // tap to start
    await expect(page.getByTestId('voice-status')).toContainText('Recording');
    await page.waitForTimeout(1200);
    await rec.click(); // tap to stop
    const transcript = page.getByTestId('transcript-text');
    await expect(transcript).toHaveValue('add review lab report tomorrow');
    await expect(page.getByTestId('transcript-review')).toContainText('Confidence 91%');
    await expect(page.getByTestId('transcript-review')).toContainText('Latency 812 ms');

    const up = api.callsTo('POST', '/api/captures/audio')[0];
    expect(up.headers['content-type']).toMatch(/^multipart\/form-data; boundary=/);
    expect(up.headers['x-csrf-token']).toBe(CSRF);
    const body = up.raw!.toString('latin1');
    expect(body).toMatch(/name="file"; filename="capture\.(webm|m4a|ogg|aac|bin)"/);
    expect(body).toMatch(/Content-Type: audio\//);
    expect(body).toMatch(/name="captured_at"\r\n\r\n\d{4}-\d{2}-\d{2}T[\d:.]+Z/);
    expect(body).toContain('name="tz"');
    expect(body).toContain('name="conversation_id"\r\n\r\nconv-1');
    expect(body).toContain('name="retain_audio"\r\n\r\nfalse');

    await transcript.fill('add review lab report tomorrow at 3pm');
    await page.getByTestId('send-transcript').click();
    await expect(page.getByText('Noted. Anything else?')).toBeVisible();
    expect(api.callsTo('POST', '/api/captures/cap-1/transcript')[0].json).toEqual({ text: 'add review lab report tomorrow at 3pm' });
    const cmd = api.callsTo('POST', '/api/commands')[0].json as Record<string, unknown>;
    expect(cmd).toMatchObject({ channel: 'voice', capture_id: 'cap-1', transcript_version: 2, text: 'add review lab report tomorrow at 3pm', speech_playing: false, conversation_id: 'conv-1' });
    expect(typeof cmd.tz).toBe('string');
  });

  test('seamless default: one tap to speak, stop → transcribed and sent without a second click', async ({ page }) => {
    const api = await setupApp(page);
    api.on('POST', '/api/captures/audio', () => ({ json: capture() }));
    api.on('POST', '/api/commands', () => ({ json: commandResponse(null, 'Noted. Anything else?') }));
    await page.goto('/console');
    await expect(page.getByLabel(/Send automatically/)).toBeChecked();
    const rec = page.getByTestId('record-btn');
    await rec.click();
    await expect(page.getByTestId('voice-status')).toContainText('Recording');
    await page.waitForTimeout(900);
    await rec.click();
    await expect(page.getByText('Noted. Anything else?')).toBeVisible();
    expect(api.callsTo('POST', '/api/commands')[0].json).toMatchObject({ channel: 'voice', capture_id: 'cap-1', transcript_version: 1 });
    await expect(page.getByTestId('send-transcript')).toHaveCount(0);
  });

  test('server audio errors (422 no_speech) are shown as voice errors, typed input stays one tap away', async ({ page }) => {
    const api = await setupApp(page);
    api.on('POST', '/api/captures/audio', () => ({ status: 422, json: { detail: { code: 'no_speech', message: 'No speech detected' } } }));
    await page.goto('/console');
    await page.getByTestId('record-btn').click();
    await expect(page.getByTestId('voice-status')).toContainText('Recording');
    await page.waitForTimeout(600);
    await page.getByTestId('record-btn').click();
    await expect(page.getByTestId('voice-error')).toContainText('Voice error (no_speech)');
    // voice first: "didn't catch that" keeps you in voice mode; typing is one tap away
    await expect(page.getByTestId('cmd-input')).toHaveCount(0);
    await page.getByTestId('type-toggle').click();
    await expect(page.getByTestId('cmd-input')).toBeEditable();
    expect(api.callsTo('POST', '/api/commands')).toHaveLength(0);
  });

  test('cancel while recording discards without upload', async ({ page }) => {
    const api = await setupApp(page);
    await page.goto('/console');
    await page.getByTestId('record-btn').click();
    await expect(page.getByTestId('voice-status')).toContainText('Recording');
    await page.getByRole('button', { name: 'Cancel (discard)' }).click();
    await expect(page.getByTestId('voice-status')).not.toContainText('Recording');
    await page.waitForTimeout(500);
    expect(api.callsTo('POST', '/api/captures/audio')).toHaveLength(0);
  });

  test('pause actions toggle: pause, banner, blocked approval, confirm resume', async ({ page }) => {
    const api = await setupApp(page);
    wireProposalFlow(api);
    await page.goto('/console');
    await sendTyped(page, 'add two tasks');
    const card = page.getByTestId('proposal-card');
    await expect(card).toBeVisible();
    await page.getByTestId('pause-toggle').click();
    await expect(page.getByTestId('paused-banner')).toContainText('All actions paused');
    await expect(page.getByTestId('pause-toggle')).toContainText('Actions paused');
    await card.getByTestId('item-0').getByRole('checkbox').check();
    await expect(card.getByTestId('approve-btn')).toBeDisabled();
    await expect(card.getByText('Actions are paused — resume from the header to approve')).toBeVisible();
    await page.getByTestId('pause-toggle').click();
    await expect(page.getByText('Earlier approvals will NOT be replayed')).toBeVisible();
    await page.getByRole('button', { name: 'Resume', exact: true }).click();
    await expect(page.getByTestId('paused-banner')).toHaveCount(0);
    await expect(card.getByTestId('approve-btn')).toBeEnabled();
    expect(api.callsTo('POST', '/api/control/pause')).toHaveLength(1);
    expect(api.callsTo('POST', '/api/control/resume')).toHaveLength(1);
  });

  test('423 paused response on approve is explained', async ({ page }) => {
    const api = await setupApp(page);
    wireProposalFlow(api);
    api.on('POST', '/api/proposals/prop-1/approve', () => ({ status: 423, json: { detail: { code: 'paused', message: 'Actions paused' } } }));
    await page.goto('/console');
    await sendTyped(page, 'add two tasks');
    const card = page.getByTestId('proposal-card');
    await card.getByTestId('item-0').getByRole('checkbox').check();
    await card.getByTestId('approve-btn').click();
    await expect(card.getByTestId('proposal-notice')).toContainText('Actions are paused. Nothing was executed.');
  });

  test('failed and unknown receipts show retry / reconciling explanation', async ({ page }) => {
    const api = await setupApp(page);
    const p = proposal();
    const failed = {
      ...p,
      status: 'partial' as const,
      items: [
        { ...p.items[0], state: 'failed' as const, last_error: 'Todoist 503' },
        { ...p.items[1], missing_fields: [], state: 'unknown' as const },
      ],
    };
    api.on('GET', '/api/proposals', () => ({ json: [failed] }));
    api.on('POST', '/api/proposals/prop-1/retry', () => ({ json: verified(failed, [0]) }));
    api.on('GET', '/api/proposals/prop-1', () => ({ json: verified(failed, [0]) }));
    await page.goto('/actions');
    const card = page.getByTestId('proposal-card');
    await expect(card.getByTestId('receipt-0')).toContainText('Todoist 503');
    await expect(card.getByTestId('receipt-1')).toContainText('may have reached todoist');
    await card.getByTestId('receipt-0').getByRole('button', { name: 'Retry this item' }).click();
    await expect.poll(() => api.callsTo('POST', '/api/proposals/prop-1/retry').length).toBe(1);
    expect(api.callsTo('POST', '/api/proposals/prop-1/retry')[0].json).toEqual({ items: [0] });
    await expect(card.getByTestId('receipt-0')).toContainText('Verified');
  });
});

test.describe('UI (mocked API) — mobile 390x844', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('record → inspect transcript → approve → receipt on a phone-sized screen', async ({ page }) => {
    const api = await setupApp(page);
    wireProposalFlow(api);
    api.on('POST', '/api/captures/audio', () => ({ json: capture() }));
    await page.goto('/');
    await expect(page.getByTestId('mission')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/mobile-today.png`, fullPage: false });
    // Bottom nav, not sidebar, on mobile.
    await expect(page.getByRole('navigation', { name: 'Primary mobile' })).toBeVisible();
    await expect(page.getByRole('complementary', { name: 'Primary' })).toBeHidden();
    await page.getByRole('navigation', { name: 'Primary mobile' }).getByRole('link', { name: 'Command' }).click();
    const rec = page.getByTestId('record-btn');
    const box = await rec.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(48); // compact orb, still above the 44px touch-target minimum
    await rec.tap();
    await expect(page.getByTestId('voice-status')).toContainText('Recording');
    await page.screenshot({ path: `${SHOTS}/mobile-recording.png` });
    await page.waitForTimeout(900);
    await page.getByRole('button', { name: 'Stop & transcribe' }).tap();
    // seamless: no separate send step
    const card = page.getByTestId('proposal-card');
    await expect(card).toBeVisible();
    await card.getByTestId('item-0').getByRole('checkbox').tap();
    await page.screenshot({ path: `${SHOTS}/mobile-proposal.png`, fullPage: true });
    await card.getByTestId('approve-btn').tap();
    await expect(card.getByTestId('receipt-0')).toContainText('Verified');
    const cmd = api.callsTo('POST', '/api/commands')[0].json as Record<string, unknown>;
    expect(cmd).toMatchObject({ channel: 'voice', capture_id: 'cap-1', transcript_version: 1 });
    await page.screenshot({ path: `${SHOTS}/mobile-receipt.png`, fullPage: true });
  });
});

test('desktop Today screenshot (mocked API)', async ({ page }) => {
  await setupApp(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto('/');
  await expect(page.getByTestId('mission')).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/desktop-today.png`, fullPage: false });
});
