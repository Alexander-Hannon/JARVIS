import { expect, test } from '@playwright/test';
import { CSRF, MockApi, setupApp } from './mockApi';

// V5 accounts UI with a MOCKED API: sign-in second step, forced enrollment, people admin, role-gated navigation,
// co-approval queue and document sharing. Server-side enforcement is covered by the backend tests.

test('sign-in asks for the second step and verifies the code', async ({ page }) => {
  const api = new MockApi();
  api.authenticated = false;
  api.on('POST', '/api/auth/login', () => ({ json: { mfa_required: true, challenge_id: 'mch_1', methods: ['recovery', 'totp'], webauthn: null } }));
  api.on('POST', '/api/auth/mfa/verify', (_r, call) => {
    const b = call.json as { code: string; method: string };
    if (b.code !== '123456') return { status: 401, json: { detail: "That code or passkey didn't check out." } };
    api.authenticated = true;
    return { json: { csrf_token: CSRF } };
  });
  await setupApp(page, api);
  await page.goto('/');
  await page.getByTestId('username').fill('aj');
  await page.getByTestId('password').fill('correct horse battery');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Second step' })).toBeVisible();
  await page.getByTestId('mfa-code').fill('123456');
  await page.getByRole('button', { name: 'Verify' }).click();
  await expect(page.getByTestId('mission')).toHaveText('Ship the JARVIS frontend');
  const verify = api.callsTo('POST', '/api/auth/mfa/verify')[0].json as Record<string, unknown>;
  expect(verify).toMatchObject({ challenge_id: 'mch_1', method: 'totp', code: '123456' });
});

test('a role that requires MFA must enroll before using JARVIS, and sees recovery codes once', async ({ page }) => {
  const api = new MockApi();
  api.mfa = { required: true, enrolled: false, satisfied: false };
  api.on('POST', '/api/auth/mfa/totp/begin', () => ({
    json: { id: 'mfa_1', secret: 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/JARVIS:aj?secret=JBSWY3DPEHPK3PXP&issuer=JARVIS' },
  }));
  api.on('POST', '/api/auth/mfa/totp/confirm', () => {
    api.mfa = { required: true, enrolled: true, satisfied: true };
    return { json: { ok: true, recovery_codes: Array.from({ length: 10 }, (_, i) => `ABCD-EF${String(i).padStart(2, '0')}`) } };
  });
  await setupApp(page, api);
  await page.goto('/');
  await expect(page.getByTestId('mfa-gate')).toBeVisible();
  await expect(page.getByTestId('mission')).toHaveCount(0);           // nothing else renders until enrolled
  await page.getByTestId('totp-begin').click();
  await expect(page.locator('.qr svg')).toBeVisible();
  await expect(page.getByTestId('totp-secret')).toContainText('JBSW Y3DP');
  await page.getByTestId('totp-code').fill('654321');
  await page.getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByTestId('recovery-codes').locator('li')).toHaveCount(10);
  await page.getByRole('button', { name: 'I saved them' }).click();
  await expect(page.getByTestId('mission')).toHaveText('Ship the JARVIS frontend');
});

test('admins invite people and get a one-time link; owners-only roles are not offered to admins', async ({ page }) => {
  const api = new MockApi();
  api.user = { ...api.user, id: 'usr_admin', username: 'ada', display_name: 'Ada', role: 'admin' };
  const users: Record<string, unknown>[] = [
    { id: 'usr_owner', username: 'aj', display_name: 'AJ', email: null, role: 'owner', status: 'active', created_at: '2026-10-01T00:00:00Z',
      last_login_at: '2026-10-05T00:00:00Z', mfa_methods: 1, active_sessions: 2, has_password: true, invite_pending_until: null },
    { id: 'usr_admin', username: 'ada', display_name: 'Ada', email: null, role: 'admin', status: 'active', created_at: '2026-10-01T00:00:00Z',
      last_login_at: '2026-10-05T00:00:00Z', mfa_methods: 1, active_sessions: 1, has_password: true, invite_pending_until: null },
  ];
  api.on('GET', '/api/users', () => ({ json: users }));
  api.on('POST', '/api/users', (_r, call) => {
    const b = call.json as { username: string; display_name: string; role: string };
    const u: Record<string, unknown> = { ...users[1], id: 'usr_new', username: b.username, display_name: b.display_name, role: b.role, mfa_methods: 0,
                active_sessions: 0, last_login_at: null, has_password: false, invite_pending_until: '2026-10-09T00:00:00Z' };
    users.push(u);
    return { json: { user: u, invite_url: 'https://jarvis.example/invite/tok_abcdefghijkl', invite_expires_hours: 72 } };
  });
  await setupApp(page, api);
  await page.goto('/settings#users');
  await expect(page.getByTestId('user-list').locator('li')).toHaveCount(2);
  const roleOptions = await page.locator('form select').first().locator('option').allTextContents();
  expect(roleOptions).not.toContain('Owner');
  expect(roleOptions).not.toContain('Admin');
  // the owner's row offers no controls to an admin
  await expect(page.getByTestId('user-list').locator('li').first().getByRole('button')).toHaveCount(0);
  await page.getByLabel('Name', { exact: true }).fill('Mia');
  await page.getByLabel('Username', { exact: true }).fill('mia');
  await page.getByRole('button', { name: 'Create invite link' }).click();
  await expect(page.getByTestId('invite-link')).toContainText('/invite/tok_abcdefghijkl');
  expect(api.callsTo('POST', '/api/users')[0].json).toMatchObject({ username: 'mia', display_name: 'Mia', role: 'member' });
});

test('auditors only see Activity, Settings and Help; members never see admin settings', async ({ page }) => {
  const api = new MockApi();
  api.user = { ...api.user, id: 'usr_aud', username: 'audrey', display_name: 'Audrey', role: 'auditor' };
  await setupApp(page, api);
  await page.goto('/activity');
  const nav = page.getByRole('complementary', { name: 'Primary' }).getByRole('link');
  await expect(nav).toHaveText([/Activity/, /Settings/, /Help/]);
  await expect(page.getByTestId('pause-toggle')).toHaveCount(0);
  await page.goto('/settings');
  await expect(page.locator('.settings-nav button')).toHaveText(['Account', 'Security']);
  // a member: their own things, no People/Backups/Diagnostics/Budget, no org pause switch
  api.user = { ...api.user, role: 'member' };
  api.on('GET', '/api/integrations/google/setup', () => ({ json: { configured: true, redirect_uri: '', required_apis: [], scopes: { calendar: [], drive: [] } } }));
  await page.goto('/settings');                   // a member lands on their own integrations
  const settingsNav = page.locator('.settings-nav');
  await expect(settingsNav).toContainText('Integrations');
  for (const admin of ['People', 'Backups', 'Diagnostics', 'Budget']) await expect(settingsNav).not.toContainText(admin);
  await expect(page.getByTestId('pause-toggle')).toHaveCount(0);
});

test('co-approval queue shows org-wide requests and sends the decision', async ({ page }) => {
  const api = new MockApi();
  api.on('GET', '/api/approvals/co', () => ({
    json: api.callsTo('POST', /\/api\/approvals\/co\//).length ? [] : [{
      id: 'prop_9', title: 'Install standup-notes', version: 1, hash: 'h9', summary: 'skill', requested_by: 'Mia', requested_username: 'mia',
      created_at: '2026-10-06T00:00:00Z', needs: ['org.integrations'],
      items: [{ index: 0, action_type: 'skill.install', args: { body: '1. Read my calendar' }, expected_effect: 'Install skill standup-notes for everyone', state: 'approved' }],
    }],
  }));
  api.on('POST', '/api/approvals/co/prop_9', () => ({ json: { status: 'queued' } }));
  await setupApp(page, api);
  await page.goto('/actions');
  await expect(page.getByTestId('co-approval')).toContainText('requested by Mia');
  await expect(page.getByTestId('co-approval')).toContainText('1. Read my calendar');
  await page.getByRole('button', { name: 'Co-approve' }).click();
  await expect(page.getByTestId('co-approval')).toHaveCount(0);
  expect(api.callsTo('POST', '/api/approvals/co/prop_9')[0].json).toEqual({ version: 1, hash: 'h9', decision: 'approve' });
});

test('documents can be shared with the organization; shared-with-you documents are read-only', async ({ page }) => {
  const api = new MockApi();
  const doc = (id: string, own: boolean, visibility: 'private' | 'org') => ({
    id, title: `${id}.pdf`, mime: 'application/pdf', origin: 'upload', project_id: null, created_at: '2026-10-06T00:00:00Z', version: 1,
    sha256: 'x', status: 'ready', processing: null, segments: 3, own, visibility,
  });
  let mine = doc('mine', true, 'private');
  api.on('GET', '/api/documents', () => ({ json: [mine, doc('theirs', false, 'org')] }));
  api.on('POST', '/api/documents/mine/visibility', () => {
    mine = { ...mine, visibility: 'org' };
    return { json: { visibility: 'org' } };
  });
  await setupApp(page, api);
  page.on('dialog', (d) => void d.accept());
  await page.goto('/memory');
  await page.getByRole('tab', { name: 'Documents' }).click();
  const theirs = page.locator('li.doc', { hasText: 'theirs.pdf' });
  await expect(theirs).toContainText('shared with you');
  await expect(theirs.getByRole('button', { name: /Delete|Share/ })).toHaveCount(0);
  await page.locator('li.doc', { hasText: 'mine.pdf' }).getByTestId('share-toggle').click();
  await expect(page.locator('li.doc', { hasText: 'mine.pdf' })).toContainText('shared with your organization');
});
