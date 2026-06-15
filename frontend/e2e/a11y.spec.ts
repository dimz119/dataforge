/**
 * a11y.spec.ts — nightly (testing-strategy §12 visual/a11y row; Phase-7 exit #7).
 *
 * "No accessibility regressions on one representative page per page group."
 * Runs @axe-core/playwright (WCAG 2.1 A/AA rule set) against one representative
 * page from each of the seven §9 page groups: auth, dashboard, workspaces,
 * scenarios, streams, apikeys, monitoring. A non-empty critical/serious violation
 * list fails the spec. This is the automated complement to the manual focus-order /
 * reduced-motion review in the P7-13 a11y pass.
 */
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

import { createWorkspace, SEED_E2E, signupAndVerify, uniqueEmail, waitForStatus } from './helpers';

/** Run axe on the current page and assert zero serious/critical violations. */
async function expectNoA11yViolations(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    // Color-contrast on the design tokens is owned by the design system (§8) and
    // tuned there; gate on structural/semantic rules (roles, names, labels, order).
    .disableRules(['color-contrast'])
    .analyze();
  const serious = results.violations.filter(
    (v) => v.impact === 'serious' || v.impact === 'critical',
  );
  expect(serious, `${label}: ${serious.map((v) => v.id).join(', ')}`).toEqual([]);
}

test.describe('@nightly a11y', () => {
  test.setTimeout(8 * 60_000);

  test('auth group — login page', async ({ page }) => {
    await page.goto('/login');
    await expect(page.getByLabel('Email')).toBeVisible();
    await expectNoA11yViolations(page, 'auth/login');
  });

  test('authenticated groups — dashboard, workspaces, scenarios, apikeys, streams, monitoring', async ({
    page,
    request,
  }) => {
    await signupAndVerify(page, request);
    const slug = await createWorkspace(page, `a11y-${uniqueEmail('ws').split('@')[0]}`);

    // dashboard group (the GettingStartedPanel is the empty-state surface).
    await expect(page.getByRole('heading', { name: /getting started/i })).toBeVisible({
      timeout: 30_000,
    });
    await expectNoA11yViolations(page, 'dashboard');

    // workspaces group — settings page (members table + danger zone).
    await page.goto(`/w/${slug}/settings`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expectNoA11yViolations(page, 'workspaces/settings');

    // scenarios group — catalog list.
    await page.goto(`/w/${slug}/scenarios`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expectNoA11yViolations(page, 'scenarios/catalog');

    // apikeys group — keys page (empty state + create CTA).
    await page.goto(`/w/${slug}/api-keys`);
    await expect(page.getByRole('button', { name: 'Create key' }).first()).toBeVisible();
    await expectNoA11yViolations(page, 'apikeys');

    // Build an instance + a stream so the streams + monitoring pages render fully.
    await page.goto(`/w/${slug}/scenarios`);
    await page.getByRole('link').filter({ hasText: /./ }).first().click();
    await page.getByRole('button', { name: 'Create instance' }).first().click();
    await page.getByLabel('Name').fill('a11y instance');
    await page.getByRole('button', { name: 'Create instance' }).last().click();
    await expect(page).toHaveURL(/\/scenarios\/instances\/[^/]+/, { timeout: 30_000 });

    await page.goto(`/w/${slug}/streams/new`);
    await page.getByLabel('Name').fill('a11y-stream');
    await page.getByLabel('Seed').fill(String(SEED_E2E));
    await page.getByLabel('Initial target rate (TPS)').fill('10');
    await page.getByRole('button', { name: 'Create stream' }).click();
    await expect(page).toHaveURL(/\/streams\/[^/]+$/, { timeout: 30_000 });

    // streams group — the control page (control matrix + TPS slider once running).
    await expectNoA11yViolations(page, 'streams/control');
    await page.getByRole('button', { name: /start/i }).click();
    await waitForStatus(page, ['running'], { timeoutMs: 120_000 });

    // monitoring group — the stream monitor with the live tail mounted.
    await page.getByRole('link', { name: 'Live tail' }).click();
    await expect(page).toHaveURL(/\/monitoring\/[^/]+$/, { timeout: 30_000 });
    await expect(page.getByRole('region', { name: 'Live event tail' })).toBeVisible({
      timeout: 60_000,
    });
    await expectNoA11yViolations(page, 'monitoring/stream');
  });
});
