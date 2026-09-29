import { expect, test } from '@playwright/test';

const PW = 'live e2e password 123';
const AJ = 'Yo Jarvis, go ahead and make a to-do-is task for me to complete my Grading for my two classes tomorrow Go ahead and give me a debrief on what my Google Calendar looks like and then tell me What the weather will look like tomorrow';

test('multi-request typed command renders sections (real backend, real weather)', async ({ page }) => {
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
  await page.goto('/console');
  await page.getByTestId('cmd-input').fill(AJ);
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('.msg-section', { hasText: 'Weather' })).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.msg-section', { hasText: 'Tasks' })).toBeVisible();
  await expect(page.locator('.msg-section', { hasText: 'Calendar' })).toBeVisible();
  await expect(page.getByText(/°F/).first()).toBeVisible();
  await page.locator('.msg-section', { hasText: 'Tasks' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'e2e-live/screenshots/multi.png' });
});
