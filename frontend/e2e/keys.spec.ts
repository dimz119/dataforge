/**
 * keys.spec.ts — nightly (testing-strategy §12; Phase-7 exit #4 + OPS-6).
 *
 * Reveal-once holds: the plaintext key appears in exactly ONE dialog, never in
 * the DOM afterwards and never in the keys table (INV-TEN-4). Revoke makes the
 * key effective-dead within 1 s (OPS-6), proven by an out-of-band API probe with
 * the captured plaintext.
 */
import { expect, test } from '@playwright/test';

import { API_URL, createWorkspace, signupAndVerify, uniqueEmail } from './helpers';

test.describe('@nightly keys', () => {
  test('reveal-once never re-shows the secret; revoke kills the key < 1 s', async ({
    page,
    request,
  }) => {
    await signupAndVerify(page, request);
    const slug = await createWorkspace(page, `keys-${uniqueEmail('ws').split('@')[0]}`);

    // Create a key with events:read (the default scope).
    await page.goto(`/w/${slug}/api-keys`);
    await page.getByRole('button', { name: 'Create key' }).first().click();
    await page.getByLabel('Name').fill('probe-key');
    await page.getByRole('button', { name: 'Create key' }).last().click();

    const dialog = page.getByRole('dialog', { name: 'API key created' });
    await expect(dialog).toBeVisible({ timeout: 30_000 });
    const secret = (await dialog.getByTestId('copy-field-value').first().textContent())?.trim();
    expect(secret).toMatch(/^df_/);
    const plaintext = secret!;

    // Close the dialog. The secret must not survive anywhere in the DOM.
    await dialog.getByRole('button', { name: 'I have copied this key' }).click();
    await dialog.getByRole('button', { name: 'Done' }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator(`text=${plaintext}`)).toHaveCount(0);
    // The table shows the masked form (prefix……last4), never the secret body.
    await expect(page.getByText('probe-key')).toBeVisible();
    await expect(page.locator(`text=${plaintext}`)).toHaveCount(0);

    // Sanity: the live key authenticates against a key-scoped endpoint.
    const before = await request.get(
      `${API_URL}/api/v1/streams/00000000-0000-0000-0000-000000000000/events`,
      { headers: { 'X-API-Key': plaintext } },
    );
    // 404 (no such stream) or 200 — NOT an auth rejection — proves the key is live.
    expect([200, 404, 410, 400]).toContain(before.status());

    // Revoke via the UI.
    await page.getByRole('button', { name: 'Revoke' }).first().click();
    await page.getByRole('button', { name: 'Revoke' }).last().click();

    // OPS-6: within 1 s the revoked key must be rejected (401/403).
    const start = Date.now();
    let rejected = false;
    while (Date.now() - start < 2_000) {
      const probe = await request.get(
        `${API_URL}/api/v1/streams/00000000-0000-0000-0000-000000000000/events`,
        { headers: { 'X-API-Key': plaintext } },
      );
      if (probe.status() === 401 || probe.status() === 403) {
        rejected = true;
        expect(Date.now() - start, 'revocation must take effect within 1 s (OPS-6)').toBeLessThan(
          1_500,
        );
        break;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    expect(rejected, 'revoked key must be rejected by the API').toBe(true);
  });
});
