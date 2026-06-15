/**
 * core-loop.spec.ts — THE Phase-7 exit criterion (@smoke, PR lane).
 *
 * A new user completes the whole loop ENTIRELY in the UI, within the PRD §2.1/§8
 * time-to-first-event budget, mirroring the demo step for step (seed 4242 =
 * SEED_E2E): account → workspace → scenario instance → reveal-once key →
 * start stream → watch live events → pause/resume → TPS slider → stop → sign out.
 *
 * Selectors prefer accessible roles/labels/text; data-testid is used only for the
 * dynamic StatusBadge / reveal secret / tail rows.
 */
import { expect, test } from '@playwright/test';

import {
  createWorkspace,
  readStatus,
  SEED_E2E,
  signupAndVerify,
  uniqueEmail,
  waitForStatus,
} from './helpers';

test.describe('@smoke core-loop', () => {
  // The whole loop, incl. waiting for a stream to reach running, fits the budget.
  test.setTimeout(8 * 60_000);

  test('account → scenario → key → stream → live events → pause/resume → stop', async ({
    page,
    request,
  }) => {
    // 1. Sign up + verify via mailpit.
    await signupAndVerify(page, request);

    // 2. Create workspace → dashboard with the GettingStartedPanel.
    const slug = await createWorkspace(page, `demo-${uniqueEmail('ws').split('@')[0]}`);
    await expect(
      page.getByRole('heading', { name: /get your first event flowing/i }),
    ).toBeVisible({
      timeout: 30_000,
    });

    // 3. Scenarios → pick the first scenario → create an instance with defaults.
    // Scope to the page body (the SideNav also renders links); the scenario cards
    // are the links inside the main content region.
    await page.goto(`/w/${slug}/scenarios`);
    await page.getByRole('main').getByRole('link').filter({ hasText: /./ }).first().click();
    await expect(page).toHaveURL(/\/scenarios\/[^/]+$/);
    await page.getByRole('button', { name: 'Create instance' }).first().click();
    await page.getByLabel('Name').fill('Core loop instance');
    await page.getByRole('button', { name: 'Create instance' }).last().click();
    // Lands on the instance config page.
    await expect(page).toHaveURL(/\/scenarios\/instances\/[^/]+/, { timeout: 30_000 });

    // 4. API key → reveal-once dialog → copy → close → assert plaintext gone.
    await page.goto(`/w/${slug}/api-keys`);
    await page.getByRole('button', { name: 'Create key' }).first().click();
    await page.getByLabel('Name').fill('core-loop-key');
    // events:read is on by default; add streams:read + streams:write.
    await page.getByLabel('streams:read').check();
    await page.getByLabel('streams:write').check();
    await page.getByRole('button', { name: 'Create key' }).last().click();

    // Reveal-once dialog shows the plaintext exactly once.
    const dialog = page.getByRole('dialog', { name: 'API key created' });
    await expect(dialog).toBeVisible({ timeout: 30_000 });
    const secret = (await dialog.getByTestId('copy-field-value').first().textContent())?.trim();
    expect(secret, 'reveal-once dialog must show a plaintext key').toBeTruthy();
    expect(secret).toMatch(/^df_/);

    // Mark copied, close, and confirm the plaintext is gone from the DOM + table.
    await dialog.getByRole('button', { name: 'I have copied this key' }).click();
    await dialog.getByRole('button', { name: 'Done' }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator(`text=${secret!}`)).toHaveCount(0);

    // 5. Create a stream (seed 4242, 10 TPS) → start → running ≤ 90 s.
    await page.goto(`/w/${slug}/streams/new`);
    await page.getByLabel('Name').fill('core-loop-stream');
    await page.getByLabel('Seed').fill(String(SEED_E2E));
    await page.getByLabel('Initial target rate (TPS)').fill('10');
    await page.getByRole('button', { name: 'Create stream' }).click();
    await expect(page).toHaveURL(/\/streams\/[^/]+$/, { timeout: 30_000 });

    await page.getByRole('button', { name: /start/i }).click();
    await waitForStatus(page, ['running'], { timeoutMs: 120_000 });

    // 6. Open the live tail → at least one event row appears.
    await page.getByRole('link', { name: 'Live tail' }).click();
    await expect(page).toHaveURL(/\/monitoring\/[^/]+$/, { timeout: 30_000 });
    await expect(page.getByTestId('tail-row').first()).toBeVisible({ timeout: 120_000 });
    // The received counter is monotonic > 0 (this connection).
    await expect(page.getByText(/received \(this connection\)/i)).toBeVisible();

    // 7. Pause display, then resume display (the tail's own pause control).
    await page.getByRole('button', { name: 'Pause display' }).click();
    await page.getByRole('button', { name: 'Resume display' }).click();

    // 8. Back to control → Pause the STREAM → paused → Resume → running.
    await page.goBack();
    await waitForStatus(page, ['running'], { timeoutMs: 30_000 });
    await page.getByRole('button', { name: 'Pause', exact: true }).click();
    await waitForStatus(page, ['paused', 'pausing'], { timeoutMs: 60_000 });
    await waitForStatus(page, ['paused'], { timeoutMs: 60_000 });
    await page.getByRole('button', { name: 'Resume', exact: true }).click();
    await waitForStatus(page, ['running', 'resuming'], { timeoutMs: 60_000 });
    await waitForStatus(page, ['running'], { timeoutMs: 60_000 });

    // 9. Stop → stopped.
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await waitForStatus(page, ['stopped', 'stopping'], { timeoutMs: 60_000 });
    await waitForStatus(page, ['stopped'], { timeoutMs: 90_000 });
    expect(await readStatus(page)).toBe('stopped');
  });
});
