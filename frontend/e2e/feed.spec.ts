// V4.1 feed behaviour with a MOCKED API: the sandbox shows inline in the Command feed, and approving internet access
// for the sandbox resumes the task automatically. Not live integration evidence.
import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { commandResponse, item, proposal, setupApp } from './mockApi';

const PNG = readFileSync(new URL('../public/icon-192.png', import.meta.url));

async function sendTyped(page: Page, text: string) {
  await page.getByTestId('cmd-input').fill(text);
  await page.getByTestId('send-btn').click();
}

test('sandbox work appears inline in the reply, with its screen and commands', async ({ page }) => {
  const api = await setupApp(page);
  await page.route('**/api/sandbox/shots/**', (r) => r.fulfill({ status: 200, contentType: 'image/png', body: PNG }));
  api.on('POST', '/api/commands', () => ({
    json: {
      ...commandResponse(null, 'Bama is favored by seven and a half.'),
      visuals: [
        {
          type: 'sandbox',
          shot_id: 'shot_abc',
          network: { active: true, domains: ['espn.com'], until: new Date(Date.now() + 600e3).toISOString() },
          steps: [{ kind: 'exec', detail: 'python3 odds.py', exit_code: 0, output: 'Bama -7.5', ms: 80 }],
          desktop_actions: 3,
        },
        { type: 'screenshot', shot_id: 'shot_abc', title: 'ESPN', url: 'https://espn.com' },
      ],
    },
  }));
  await page.goto('/console');
  await sendTyped(page, 'check the odds in the workbench');
  const card = page.getByTestId('viz-sandbox');
  await expect(card).toBeVisible();
  await expect(card).toContainText('online to espn.com, 3 desktop actions');
  await expect(card).toContainText('$ python3 odds.py');
  await expect(card).toContainText('Bama -7.5');
  await expect(card.locator('img')).toHaveJSProperty('naturalWidth', 192);
  await expect(page.locator('.viz-shot')).toHaveCount(0); // no duplicate screenshot card
  await page.setViewportSize({ width: 1366, height: 900 });
  await card.screenshot({ path: 'e2e/screenshots/v41-feed-sandbox.png' });
});

test('approving sandbox internet access resumes the task in the same feed', async ({ page }) => {
  const api = await setupApp(page);
  const net = proposal({
    id: 'prop-net',
    title: 'Let the sandbox reach espn.com',
    items: [item(0, { action_type: 'sandbox.network', provider: 'sandbox', target: { id: 'sandbox', name: 'Sandbox' },
      args: { domains: ['espn.com', 'rolltide.com'], minutes: 30 }, expected_effect: 'Let the sandbox reach espn.com, rolltide.com for 30 min' })],
  });
  let commands = 0;
  api.on('POST', '/api/commands', () => {
    commands += 1;
    return { json: commands === 1 ? commandResponse(net, 'I need internet access to ESPN first.') : { ...commandResponse(null, 'Here is the game: Bama vs Mississippi State, 6 PM.'), run_id: 'run-2' } };
  });
  api.on('POST', '/api/proposals/prop-net/approve', () => ({ json: { approval_id: 'a1', proposal: { ...net, status: 'queued', items: [{ ...net.items[0], state: 'queued' }] } } }));
  api.on('GET', '/api/proposals/prop-net', () => ({ json: { ...net, status: 'verified', items: [{ ...net.items[0], state: 'verified' }] } }));
  await page.goto('/console');
  await sendTyped(page, 'look up the Alabama game in the workbench');
  const card = page.getByTestId('proposal-card');
  await card.getByTestId('item-0').getByRole('checkbox').check();
  await card.getByTestId('approve-btn').click();
  await expect(page.getByText('Here is the game: Bama vs Mississippi State, 6 PM.')).toBeVisible();
  const second = api.callsTo('POST', '/api/commands')[1].json as { text: string };
  expect(second.text).toBe('Sandbox internet access to espn.com, rolltide.com is approved and open. Continue the task you were doing.');
  await page.waitForTimeout(1500);
  expect(api.callsTo('POST', '/api/commands')).toHaveLength(2); // resumes exactly once
});
