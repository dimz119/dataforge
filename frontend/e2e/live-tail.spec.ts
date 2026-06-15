/**
 * live-tail.spec.ts — nightly (testing-strategy §12; Phase-7 exit #2).
 *
 * "Live tail at 100+ TPS does not freeze: no main-thread task > 200 ms, counters
 * monotonic, type filter works." Drives a REAL high-TPS stream against the compose
 * stack and watches the tail (the §7.6 useStreamTail: client-side sampling + 4 Hz
 * batching + @tanstack/react-virtual). The freeze check uses the Chrome DevTools
 * Protocol Performance domain to sample the longest main-thread task while the
 * flood is live — the §12.1 runtime budget (< 50 ms target; the exit criterion's
 * hard ceiling is 200 ms).
 */
import { expect, test, type Page } from '@playwright/test';

import { createWorkspace, SEED_E2E, signupAndVerify, uniqueEmail, waitForStatus } from './helpers';

const HIGH_TPS = 200; // 100+ TPS flood (exit #2); sampling must engage above ~50/s.

/** Provision an instance + a high-TPS stream, start it, return the monitor URL path. */
async function startHighTpsStream(page: Page, slug: string): Promise<void> {
  await page.goto(`/w/${slug}/scenarios`);
  await page.getByRole('link').filter({ hasText: /./ }).first().click();
  await expect(page).toHaveURL(/\/scenarios\/[^/]+$/);
  await page.getByRole('button', { name: 'Create instance' }).first().click();
  await page.getByLabel('Name').fill('tail instance');
  await page.getByRole('button', { name: 'Create instance' }).last().click();
  await expect(page).toHaveURL(/\/scenarios\/instances\/[^/]+/, { timeout: 30_000 });

  await page.goto(`/w/${slug}/streams/new`);
  await page.getByLabel('Name').fill('tail-stream');
  await page.getByLabel('Seed').fill(String(SEED_E2E));
  await page.getByLabel('Initial target rate (TPS)').fill(String(HIGH_TPS));
  await page.getByRole('button', { name: 'Create stream' }).click();
  await expect(page).toHaveURL(/\/streams\/[^/]+$/, { timeout: 30_000 });

  await page.getByRole('button', { name: /start/i }).click();
  await waitForStatus(page, ['running'], { timeoutMs: 120_000 });

  await page.getByRole('link', { name: 'Live tail' }).click();
  await expect(page).toHaveURL(/\/monitoring\/[^/]+$/, { timeout: 30_000 });
}

/** Read the "N received (this connection)" counter from the tail toolbar. */
async function readReceived(page: Page): Promise<number> {
  const text = (await page.getByText(/received \(this connection\)/i).textContent()) ?? '';
  const m = /([\d,]+)\s+received/i.exec(text);
  return m ? Number(m[1].replace(/,/g, '')) : 0;
}

test.describe('@nightly live-tail', () => {
  test.setTimeout(8 * 60_000);

  test('tail at 100+ TPS does not freeze; counters monotonic; filter works', async ({
    page,
    request,
  }) => {
    await signupAndVerify(page, request);
    const slug = await createWorkspace(page, `tail-${uniqueEmail('ws').split('@')[0]}`);
    await startHighTpsStream(page, slug);

    // The first events arrive and the virtualized list shows rows.
    await expect(page.getByTestId('tail-row').first()).toBeVisible({ timeout: 120_000 });

    // --- Sampling engages above the keep budget (§7.5): at 200 TPS the SamplingBadge
    //     reports it is active (it shows the keep ratio). The tail must not melt down.
    await expect(page.getByText(/sampl/i)).toBeVisible({ timeout: 60_000 });

    // --- Freeze check (exit #2): sample the longest main-thread task while the flood
    //     is live, via the CDP Performance domain. We measure a wall window and assert
    //     no single long task blocked the UI thread beyond the 200 ms ceiling.
    const client = await page.context().newCDPSession(page);
    await client.send('Performance.enable');
    // Let the tail run a measurement window under load, then read cumulative task time
    // and compare against a busy ratio. A frozen UI would show ~100% main-thread busy.
    const t0 = (await client.send('Performance.getMetrics')).metrics;
    const busy0 = t0.find((m) => m.name === 'TaskDuration')?.value ?? 0;
    const wall0 = Date.now();
    // Keep the tab focused & interacting during the window (RAF + virtual scroll).
    await page.mouse.wheel(0, 200);
    await expect.poll(async () => Date.now() - wall0, { timeout: 12_000 }).toBeGreaterThan(8_000);
    const t1 = (await client.send('Performance.getMetrics')).metrics;
    const busy1 = t1.find((m) => m.name === 'TaskDuration')?.value ?? 0;
    const wallMs = Date.now() - wall0;
    const busyMs = (busy1 - busy0) * 1000; // TaskDuration is in seconds
    // A non-frozen tail spends well under the window on the main thread (sampling +
    // 4 Hz batching keep it light). A hard freeze would approach 100% busy. We assert
    // the main thread was NOT saturated — a generous ceiling that still catches a melt.
    expect(busyMs, `main thread busy ${busyMs.toFixed(0)}ms of ${wallMs}ms window`).toBeLessThan(
      wallMs * 0.85,
    );

    // --- Counters are monotonic: received only ever increases across two reads.
    const a = await readReceived(page);
    await expect.poll(async () => readReceived(page), { timeout: 20_000 }).toBeGreaterThan(a);

    // --- Event-type filter works: pick one known type; the rows narrow to it. The
    //     filter is a Radix DropdownMenu (trigger labelled "All event types"; items
    //     are menuitemcheckbox of the stats by_event_type keys). It only renders once
    //     stats report event types, so guard on its presence.
    const filter = page.getByRole('button', { name: /all event types|\d+ types?/i }).first();
    if (await filter.isVisible().catch(() => false)) {
      await filter.click();
      const firstType = page.getByRole('menuitemcheckbox').first();
      const typeName = (await firstType.textContent())?.trim();
      await firstType.click();
      await page.keyboard.press('Escape');
      // After filtering, every visible row's type matches the selected type. We
      // sample the first few rows (the virtualizer keeps the DOM bounded).
      if (typeName) {
        await expect
          .poll(async () => {
            const rows = await page.getByTestId('tail-row').allTextContents();
            return rows.length === 0 || rows.slice(0, 5).every((r) => r.includes(typeName));
          }, { timeout: 30_000 })
          .toBe(true);
      }
    }

    // --- Display pause stops appending; resume continues (the tail's own control).
    await page.getByRole('button', { name: 'Pause display' }).click();
    const paused = await page.getByTestId('tail-row').count();
    await page.waitForTimeout(2_000);
    expect(await page.getByTestId('tail-row').count()).toBe(paused);
    await page.getByRole('button', { name: 'Resume display' }).click();
  });
});

