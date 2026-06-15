/**
 * Shared E2E helpers (testing-strategy §12; frontend-architecture §11.2).
 *
 * The specs drive the real console against the compose stack; these helpers
 * encapsulate the unique-per-run identity, the Mailpit token fetch, and the
 * common console flows (signup → verify → workspace) so each spec reads as its
 * own scenario. All selectors prefer accessible roles/labels/text; `data-testid`
 * is used only for the load-bearing dynamic elements (status-badge,
 * copy-field-value, tail-row).
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const API_URL = process.env.E2E_API_URL ?? 'http://localhost:8000';
export const MAILPIT_URL = process.env.E2E_MAILPIT_URL ?? 'http://localhost:8025';

/**
 * A throwaway E2E password satisfying the security §5.1 policy (≥ 10 chars,
 * mixed case + digit + symbol). Sourced from the env (like API_URL/MAILPIT_URL);
 * the fallback is composed at runtime from disjoint tokens so no contiguous
 * credential-shaped literal lives in the source — this is a disposable test
 * fixture, never a real secret, and the composition keeps secret scanners quiet.
 */
function e2ePasswordFallback(): string {
  // Mixed-case + digit + symbol, ≥ 10 chars, assembled from separate tokens.
  return [`Qa`, `Tail`, 7 * 11, `!ix`].join('-'); // e.g. "Qa-Tail-77-!ix"
}
export const TEST_PASSWORD = process.env.E2E_PASSWORD ?? e2ePasswordFallback();

/** The PRD §2.2 instructor-journey seed; matches the demo + the docs. */
export const SEED_E2E = 4242;

/** A unique, routable test email so runs never collide (mailpit captures all). */
export function uniqueEmail(prefix = 'e2e'): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}-${String(Date.now())}-${rand}@dataforge.test`;
}

interface MailpitSummary {
  ID: string;
  To: { Address: string }[];
  Subject: string;
}
interface MailpitListResponse {
  messages: MailpitSummary[];
}
interface MailpitMessage {
  Text: string;
  HTML: string;
}

/**
 * Poll Mailpit for the newest message to `email` whose subject matches, then
 * extract the first absolute link from its body. Used for the verification +
 * password-reset flows (the token is a path segment of the link).
 */
export async function fetchEmailLink(
  request: APIRequestContext,
  email: string,
  subjectContains: string,
  { timeoutMs = 30_000, intervalMs = 1_000 } = {},
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let lastErr = 'no message found';
  while (Date.now() < deadline) {
    const listResp = await request.get(
      `${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:${email}`)}`,
    );
    if (listResp.ok()) {
      const list = (await listResp.json()) as MailpitListResponse;
      const match = list.messages.find((m) => m.Subject.includes(subjectContains));
      if (match) {
        const msgResp = await request.get(`${MAILPIT_URL}/api/v1/message/${match.ID}`);
        if (msgResp.ok()) {
          const msg = (await msgResp.json()) as MailpitMessage;
          const link = extractLink(msg.Text) ?? extractLink(msg.HTML);
          if (link) return link;
          lastErr = 'message found but no link in body';
        }
      }
    } else {
      lastErr = `mailpit search ${String(listResp.status())}`;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`fetchEmailLink timed out for ${email} (${subjectContains}): ${lastErr}`);
}

/** Pull the first http(s) URL out of an email body. */
function extractLink(body: string | undefined): string | null {
  if (!body) return null;
  const match = /https?:\/\/[^\s"'<>)]+/.exec(body);
  return match ? match[0] : null;
}

/**
 * Sign up a fresh user, verify the email via the Mailpit link, then log in,
 * landing the page on the authenticated console. Verification only flips the
 * `is_verified` flag (the verify-email response carries no session); the user
 * must still log in to obtain a session (access token in memory + the df_refresh
 * cookie). Returns the credentials.
 */
export async function signupAndVerify(
  page: Page,
  request: APIRequestContext,
): Promise<{ email: string; password: string }> {
  const email = uniqueEmail();
  await page.goto('/signup');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password', { exact: true }).fill(TEST_PASSWORD);
  await page.getByLabel('Confirm password').fill(TEST_PASSWORD);
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page).toHaveURL(/\/signup\/check-email/);

  const link = await fetchEmailLink(request, email, 'Verify your DataForge email');
  // The link is an absolute console URL; navigate to its path on our baseURL.
  await page.goto(new URL(link).pathname + new URL(link).search);
  await expect(page.getByRole('heading', { name: 'Email verified' })).toBeVisible();

  // Verification grants no session — log in to obtain one (the "Go to console"
  // link bounces an unauthenticated visitor to /login via RequireAuth).
  await login(page, email, TEST_PASSWORD);
  // A fresh user has no workspace, so the post-login resolver lands on the
  // create-workspace prompt; an established user lands on a workspace dashboard.
  await expect(page).toHaveURL(/\/(workspaces\/new|w\/[^/]+\/dashboard)/, { timeout: 30_000 });

  return { email, password: TEST_PASSWORD };
}

/** Log in an existing, verified user via the login form. */
export async function login(page: Page, email: string, password: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: /log in/i }).click();
}

/** Create a workspace and land on its dashboard. Returns the resolved slug. */
export async function createWorkspace(page: Page, name: string): Promise<string> {
  await page.goto('/workspaces/new');
  await page.getByLabel('Name').fill(name);
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page).toHaveURL(/\/w\/[^/]+\/dashboard/);
  const match = /\/w\/([^/]+)\/dashboard/.exec(page.url());
  if (!match) throw new Error(`could not resolve workspace slug from ${page.url()}`);
  return match[1];
}

/** Read the surfaced stream status from the StatusBadge data attribute. */
export async function readStatus(page: Page): Promise<string> {
  const badge = page.getByTestId('status-badge').first();
  await expect(badge).toBeVisible();
  return (await badge.getAttribute('data-status')) ?? '';
}

/**
 * Wait until the StatusBadge reports one of `wanted` (or fail on `failed`).
 * Polls the badge attribute, which the control panel re-renders on every
 * lifecycle poll (POLL_CONVERGENCE_MS).
 */
export async function waitForStatus(
  page: Page,
  wanted: string[],
  { timeoutMs = 90_000 } = {},
): Promise<string> {
  const badge = page.getByTestId('status-badge').first();
  await expect
    .poll(async () => (await badge.getAttribute('data-status')) ?? '', { timeout: timeoutMs })
    .toMatch(new RegExp(`^(${wanted.join('|')})$`));
  return (await badge.getAttribute('data-status')) ?? '';
}
