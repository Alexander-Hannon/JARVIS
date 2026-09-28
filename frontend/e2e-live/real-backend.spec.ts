import { expect, test } from '@playwright/test';

const PW = process.env.JARVIS_TEST_PASSWORD ?? 'live e2e password 123';

test('real mic audio → local STT → editable transcript → proposal with truthful blockers', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Set up your owner password|Sign in/ })).toBeVisible();
  const setup = page.getByRole('heading', { name: 'Set up your owner password' });
  if (await setup.isVisible()) {
    await page.getByLabel('New password').fill(PW);
    await page.getByLabel('Confirm password').fill(PW);
    await page.getByRole('button', { name: /set up|create|continue/i }).click();
  } else {
    await page.getByTestId('password').fill(PW);
    await page.getByRole('button', { name: 'Sign in' }).click();
  }
  await expect(page.getByRole('complementary', { name: 'Primary' })).toBeVisible();
  await page.goto('/console');
  const rec = page.getByRole('button', { name: /Record voice command/ });
  await rec.click();
  await page.waitForTimeout(9500); // the fake device plays the 8.7 s utterance
  await page.getByRole('button', { name: /Stop & transcribe|Stop recording/ }).first().click();
  const transcript = page.getByTestId('transcript-text');
  await expect(transcript).toBeVisible({ timeout: 60_000 });
  const text = (await transcript.inputValue()).toLowerCase();
  for (const w of ['grade', 'homework', 'tomorrow', 'groceries', 'tonight']) expect(text).toContain(w);
  await page.screenshot({ path: 'e2e-live/screenshots/transcript.png', fullPage: true });
  await transcript.fill(await transcript.inputValue()); // editable
  await page.getByRole('button', { name: 'Send as command' }).click();
  await expect(page.getByText(/tasks? ready for Todoist/).first()).toBeVisible();
  await expect(page.getByText(/Todoist not connected/).first()).toBeVisible();
  await page.screenshot({ path: 'e2e-live/screenshots/proposal.png', fullPage: true });
  // Today page reflects disconnected sources truthfully
  await page.goto('/');
  await expect(page.getByText(/not connected/i).first()).toBeVisible();
  await page.screenshot({ path: 'e2e-live/screenshots/today.png', fullPage: true });
});
