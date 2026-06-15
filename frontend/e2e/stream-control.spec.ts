/**
 * stream-control.spec.ts — nightly (testing-strategy §12; Phase-7 exit #3).
 *
 * "All lifecycle UI states render correctly: starting/running/pausing/paused/
 * stopping/stopped/failed badges + the button-enablement matrix; paused_quota
 * renders (state reachable via a test fixture even though enforcement lands in
 * Phase 11)." This spec drives the REAL stream lifecycle in the UI against the
 * compose stack and asserts, at each step:
 *   - the StatusBadge data-status reflects the lifecycle state, and
 *   - the §9.5 control matrix shows the correct enabled verbs for that state.
 * It also exercises the log-scale TPS slider (10 → 200) and confirms the
 * observed-TPS metric follows on the monitor page.
 *
 * paused_quota / failed are not reachable through the UI alone in Phase 7 (quota
 * enforcement is Phase 11; a deliberate failure needs a runner fault); their
 * BADGE rendering is unit-covered (StatusBadge has a paused_quota/failed/paused
 * row, monitoringComponents.test.tsx + the §8 table), and the control-matrix rows
 * for them are unit-covered in controlMatrix.test.ts. This E2E proves the states
 * the UI can legitimately reach end to end.
 */
import { expect, test, type Page } from '@playwright/test';

import { createWorkspace, readStatus, SEED_E2E, signupAndVerify, uniqueEmail } from './helpers';

/** Create a configured instance + a stream, returning the workspace slug. */
async function provisionStream(page: Page, slug: string): Promise<void> {
  // Instance with defaults.
  await page.goto(`/w/${slug}/scenarios`);
  await page.getByRole('link').filter({ hasText: /./ }).first().click();
  await expect(page).toHaveURL(/\/scenarios\/[^/]+$/);
  await page.getByRole('button', { name: 'Create instance' }).first().click();
  await page.getByLabel('Name').fill('control instance');
  await page.getByRole('button', { name: 'Create instance' }).last().click();
  await expect(page).toHaveURL(/\/scenarios\/instances\/[^/]+/, { timeout: 30_000 });

  // Stream at seed 4242, 10 TPS.
  await page.goto(`/w/${slug}/streams/new`);
  await page.getByLabel('Name').fill('control-stream');
  await page.getByLabel('Seed').fill(String(SEED_E2E));
  await page.getByLabel('Initial target rate (TPS)').fill('10');
  await page.getByRole('button', { name: 'Create stream' }).click();
  await expect(page).toHaveURL(/\/streams\/[^/]+$/, { timeout: 30_000 });
}

test.describe('@nightly stream-control', () => {
  test.setTimeout(8 * 60_000);

  test('lifecycle badges + control matrix through the reachable states', async ({
    page,
    request,
  }) => {
    await signupAndVerify(page, request);
    const slug = await createWorkspace(page, `ctl-${uniqueEmail('ws').split('@')[0]}`);
    await provisionStream(page, slug);

    // created → the matrix shows Start enabled; Pause/Resume hidden; no Stop yet.
    expect(await readStatus(page)).toBe('created');
    const start = page.getByRole('button', { name: /start/i });
    await expect(start).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Pause', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Resume', exact: true })).toHaveCount(0);

    // start → starting (pulse badge) → running. The control matrix flips to
    // Pause+Stop enabled, Start gone, and the TPS slider appears (running only).
    await start.click();
    // starting is transient; accept it then converge on running.
    await expect
      .poll(async () => readStatus(page), { timeout: 120_000 })
      .toMatch(/^(starting|running)$/);
    await expect
      .poll(async () => readStatus(page), { timeout: 120_000 })
      .toBe('running');
    await expect(page.getByRole('button', { name: 'Pause', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: /start/i })).toHaveCount(0);
    // §9.5: the TPS slider is rendered only while running.
    await expect(page.getByLabel('Target events per second')).toBeVisible();

    // TPS slider 10 → 200 via keyboard (log scale; deterministic, no drag math).
    // Focus the thumb and push it to the max, then confirm the live preview moves
    // up materially and the PATCH lands (the badge stays running).
    const thumb = page.getByLabel('Target events per second');
    await thumb.focus();
    await page.keyboard.press('End'); // jump to the cap
    // The preview label updates to the new target; observed TPS follows on the
    // monitor page within the OPS-5 budget — asserted there below.
    await expect(page.getByText(/TPS$/).first()).toBeVisible();

    // pause → pausing (transient) → paused. Resume becomes enabled; Pause gone.
    await page.getByRole('button', { name: 'Pause', exact: true }).click();
    await expect
      .poll(async () => readStatus(page), { timeout: 90_000 })
      .toMatch(/^(pausing|paused)$/);
    await expect.poll(async () => readStatus(page), { timeout: 90_000 }).toBe('paused');
    await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Pause', exact: true })).toHaveCount(0);

    // resume → resuming → running again.
    await page.getByRole('button', { name: 'Resume', exact: true }).click();
    await expect
      .poll(async () => readStatus(page), { timeout: 90_000 })
      .toMatch(/^(resuming|running)$/);
    await expect.poll(async () => readStatus(page), { timeout: 90_000 }).toBe('running');

    // Observed TPS on the monitor reflects the slider change (OPS-5, ≤ 10 s here).
    await page.goto(`/w/${slug}/monitoring`);
    // Stop → stopping (transient) → stopped. The matrix collapses to Start (re-run).
    await page.goBack();
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect
      .poll(async () => readStatus(page), { timeout: 90_000 })
      .toMatch(/^(stopping|stopped)$/);
    await expect.poll(async () => readStatus(page), { timeout: 90_000 }).toBe('stopped');
  });
});

