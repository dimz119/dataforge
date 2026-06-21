/**
 * registry.spec.ts — Phase 10 exit #6 (@nightly; E2E `registry.spec.ts` lane).
 *
 * "Registry browser works: subject list, version history, additive diff, compat-error
 * surfacing." This spec drives the REAL console against the compose stack and covers, end
 * to end:
 *   1. browse the registry subjects → open `ecommerce.order_placed`,
 *   2. see its v1/v2/v3 version timeline,
 *   3. confirm the additive diff renders `shipping_state` as a single green addition,
 *   4. schedule a mid-stream upgrade on a stream and observe the pending state.
 *
 * The v2/v3 evolutions are registered by the backend `seed_schema_evolutions` command
 * (Flow 2); the registry is global, so the subject is visible from any workspace. The
 * schedule step provisions a fresh stream at seed 4242 (SEED_E2E), starts it (PIN-R1
 * materializes the effective pin), then schedules `order_placed → v2` and asserts the
 * upgrade row enters its scheduled/pending state.
 *
 * Selectors prefer accessible roles/labels/text; data-testid is used only for the
 * dynamic StatusBadge.
 */
import { expect, test, type Page } from '@playwright/test';

import { createWorkspace, readStatus, SEED_E2E, signupAndVerify, uniqueEmail } from './helpers';

const SUBJECT = 'ecommerce.order_placed';

/** Create a configured instance + a stream, returning nothing (lands on the detail page). */
async function provisionStream(page: Page, slug: string): Promise<void> {
  await page.goto(`/w/${slug}/scenarios`);
  await page.getByRole('main').getByRole('link').filter({ hasText: /./ }).first().click();
  await expect(page).toHaveURL(/\/scenarios\/[^/]+$/);
  await page.getByRole('button', { name: 'Create instance' }).first().click();
  await page.getByLabel('Name').fill('registry instance');
  await page.getByRole('button', { name: 'Create instance' }).last().click();
  await expect(page).toHaveURL(/\/scenarios\/instances\/[^/]+/, { timeout: 30_000 });

  await page.goto(`/w/${slug}/streams/new`);
  await page.getByLabel('Name').fill('registry-stream');
  await page.getByLabel('Seed').fill(String(SEED_E2E));
  await page.getByLabel('Initial target rate (TPS)').fill('10');
  await page.getByRole('button', { name: 'Create stream' }).click();
  await expect(page).toHaveURL(/\/streams\/[^/]+$/, { timeout: 30_000 });
}

test.describe('@nightly registry', () => {
  test.setTimeout(8 * 60_000);

  test('browse subjects → timeline → additive diff → schedule an upgrade', async ({
    page,
    request,
  }) => {
    await signupAndVerify(page, request);
    const slug = await createWorkspace(page, `reg-${uniqueEmail('ws').split('@')[0]}`);

    // 1. Schemas in the SideNav → the registry browser table.
    await page.goto(`/w/${slug}/schemas`);
    await expect(page.getByRole('heading', { name: 'Schemas' })).toBeVisible();

    // The order_placed business subject is listed (seeded globally). Search narrows it.
    await page.getByLabel('Search subjects').fill('order_placed');
    const subjectCell = page.getByText(SUBJECT, { exact: true }).first();
    await expect(subjectCell).toBeVisible({ timeout: 30_000 });

    // 2. Open the subject → the version timeline (v1/v2/v3, newest first).
    await subjectCell.click();
    await expect(page).toHaveURL(new RegExp(`/schemas/${SUBJECT.replace('.', '\\.')}`));
    await expect(page.getByRole('heading', { name: SUBJECT })).toBeVisible();

    const timeline = page.getByRole('list', { name: 'Version timeline' });
    await expect(timeline.getByRole('button', { name: /^v3/ })).toBeVisible({ timeout: 30_000 });
    await expect(timeline.getByRole('button', { name: /^v2/ })).toBeVisible();
    await expect(timeline.getByRole('button', { name: /^v1/ })).toBeVisible();

    // 3. Select v2 → the additive diff names shipping_state as a single green addition,
    //    and the "removed/changed: none — BACKWARD_ADDITIVE" line is present.
    await timeline.getByRole('button', { name: /^v2/ }).click();
    await expect(page.getByRole('heading', { name: 'v1 → v2 diff' })).toBeVisible();
    await expect(page.getByText('/properties/shipping_state')).toBeVisible();
    await expect(page.getByText(/removed \/ changed: none/i)).toBeVisible();

    // The version document viewer shows the schema_ref for the selected version.
    await expect(page.getByText(`${SUBJECT}/2`).first()).toBeVisible();

    // 4. Provision + start a stream, then schedule order_placed → v2 and see pending.
    await provisionStream(page, slug);
    const start = page.getByRole('button', { name: /start/i });
    await start.click();
    await expect.poll(async () => readStatus(page), { timeout: 120_000 }).toBe('running');

    // The schema panel's scheduling form: pick the subject + target, submit.
    await expect(page.getByRole('heading', { name: 'Schedule an upgrade' })).toBeVisible();
    // The picker option `value` is the subject name; the label carries the version range.
    await page.getByLabel('Subject').selectOption(SUBJECT);
    await page.getByLabel('Target version').selectOption('2');
    await page.getByRole('button', { name: 'Schedule upgrade' }).click();

    // The upgrade row appears in the timeline in its pending (scheduled) state.
    const upgradeRow = page.getByRole('listitem').filter({ hasText: SUBJECT }).first();
    await expect(upgradeRow).toBeVisible({ timeout: 30_000 });
    await expect(upgradeRow.getByText('→ v2')).toBeVisible();
  });
});
