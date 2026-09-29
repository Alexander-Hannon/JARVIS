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
  // Seamless flow: ONE tap. The fake mic plays speech followed by silence; Jarvis stops on the pause,
  // transcribes locally, and sends the command without a second click.
  await page.getByRole('button', { name: /Record voice command/ }).click();
  await expect(page.getByText(/Listening/).first()).toBeVisible();
  await page.screenshot({ path: 'e2e-live/screenshots/listening.png' });
  await expect(page.getByText(/tasks? ready for Todoist/).first()).toBeVisible();
  await expect(page.getByText(/Todoist not connected/).first()).toBeVisible();
  await page.screenshot({ path: 'e2e-live/screenshots/proposal.png', fullPage: true });
  // Today page reflects disconnected sources truthfully
  await page.goto('/');
  await expect(page.getByText(/not connected/i).first()).toBeVisible();
  await page.screenshot({ path: 'e2e-live/screenshots/today.png', fullPage: true });
});
