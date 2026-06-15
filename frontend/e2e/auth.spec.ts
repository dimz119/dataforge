/**
 * auth.spec.ts — PR smoke (testing-strategy §12, frontend-architecture §11.2).
 *
 * Signup → email verify (mailpit) → login → token refresh → logout, entirely in
 * the UI against the compose stack. The access token lives in memory only; the
 * refresh is exercised by a full page reload (which has no access token and must
 * re-bootstrap the session from the df_refresh HttpOnly cookie, §6.2/§6.3).
 */
import { expect, test } from '@playwright/test';

import { createWorkspace, login, signupAndVerify, uniqueEmail } from './helpers';

test.describe('@smoke auth', () => {
  test('signup → verify → login → refresh → logout', async ({ page, request }) => {
    // 1. Signup + verify via the mailpit link, then log in (signupAndVerify logs
    //    in; verification alone grants no session — the access token in memory +
    //    the df_refresh cookie come from login, §6.2/§6.3). The user email proves
    //    authentication, but the UserMenu only renders inside the workspace shell,
    //    so create a workspace first.
    const { email } = await signupAndVerify(page, request);
    await createWorkspace(page, `auth-${uniqueEmail('ws').split('@')[0]}`);
    // The workspace shell shows the user menu with the email — proof of auth.
    await expect(page.getByText(email)).toBeVisible({ timeout: 30_000 });

    // 2. Token refresh: reload drops the in-memory access token; the session must
    //    re-bootstrap from the df_refresh cookie and stay authenticated (§6.2).
    await page.reload();
    await expect(page.getByText(email)).toBeVisible({ timeout: 30_000 });
    await expect(page).not.toHaveURL(/\/login/);

    // 3. Logout → /login, and a reload now stays unauthenticated (cookie cleared).
    await page.getByText(email).click();
    await page.getByRole('menuitem', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });

    await page.reload();
    await expect(page).toHaveURL(/\/login/);
  });

  test('invalid login surfaces an error and does not authenticate', async ({ page }) => {
    await login(page, 'nobody@dataforge.test', 'wrong-password-123');
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/\/login/);
  });
});
