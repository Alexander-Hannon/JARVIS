import { expect, test, type Page } from '@playwright/test';

// Real backend. Proves: replies auto-speak (local Jarvis voice requested with no extra click),
// and voice speed saves server-side so a brand-new browser (e.g. the phone) gets the same setting.
const PW = process.env.JARVIS_TEST_PASSWORD ?? 'live e2e password 123';
async function login(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Set up your owner password|Sign in/ })).toBeVisible();
  if (await page.getByRole('heading', { name: 'Set up your owner password' }).isVisible()) {
    await page.getByLabel('New password').fill(PW);
    await page.getByLabel('Confirm password').fill(PW);
    await page.getByRole('button', { name: /Create owner account/ }).click();
  } else {
    await page.getByTestId('password').fill(PW);
    await page.getByRole('button', { name: 'Sign in' }).click();
  }
  await expect(page.getByRole('complementary', { name: 'Primary' })).toBeVisible();
}

test('replies are spoken automatically in the Jarvis voice', async ({ page }) => {
  await login(page);
  await page.goto('/console');
  const tts = page.waitForRequest((r) => r.url().endsWith('/api/voice/tts') && r.method() === 'POST', { timeout: 30000 });
  if (!(await page.getByTestId('cmd-input').isVisible())) await page.getByTestId('type-toggle').click();
  await page.getByTestId('cmd-input').fill("what's the weather tomorrow");
  await page.getByTestId('send-btn').click();
  const req = await tts;
  const body = req.postDataJSON() as { text: string; preset: string; rate: number };
  expect(body.preset).toBe('jarvis');
  expect(body.text).not.toMatch(/[*#▸]/);
  const res = await req.response();
  expect(res?.headers()['content-type']).toBe('audio/wav');
});

test('voice speed persists across browsers (server-synced)', async ({ page, browser }) => {
  await login(page);
  await page.goto('/settings#voice');
  const slider = page.locator('input[type=range]').first();
  await slider.fill('1.35');
  await page.waitForTimeout(800);
  const ctx2 = await browser.newContext({ permissions: ['microphone'] });   // fresh storage, like another device
  const p2 = await ctx2.newPage();
  await login(p2);
  await p2.goto('/settings#voice');
  await expect(p2.getByText('Speed 1.35×')).toBeVisible({ timeout: 10000 });
  await ctx2.close();
});
