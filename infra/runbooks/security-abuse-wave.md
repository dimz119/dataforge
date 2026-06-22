# Security incident runbook: Abuse wave (signup/auth flood, request flood)

A spike of malicious traffic: credential-stuffing / brute-force auth, a signup flood
(disposable-email farms, bot registration), or a request flood against the API. The
controls are layered — per-IP signup windows + disposable-email denylist (always on), the
captcha hook (config-flip), and per-key rate limits (config-flip in prod) — escalating from
"already absorbing it" to "tighten the screws."

**Severity:** sev-2 (sev-1 if it degrades availability for legitimate tenants — correlate
with `ApiAvailabilityBurn`).

## Detect

| Signal | Metric / source | Means |
|---|---|---|
| Auth-failure spike | `df_auth_failures_total{mechanism,reason}` → `AuthFailureSpike` (ticket; `rate[5m] > 50`) | credential stuffing / brute force |
| Rate-limit trips climbing | `df_rate_limited_total{scope}` | clients (or attackers) over a per-key bucket |
| Signup surge | new-user rate; many `is_disposable`-rejected attempts | signup/registration abuse |
| 5xx / latency burn | `df_http_requests_total{status=~"5.."}`, `ApiAvailabilityBurn` | flood degrading availability |

1. `AuthFailureSpike` (observability §9; `infra/observability/prometheus/slo-alerts.yml`) is
   the primary signal — `sum(rate(df_auth_failures_total[5m])) > 50`. Split by `reason`
   (`invalid_token`, `invalid-api-key`, …) and `mechanism` (`jwt` vs key) to characterize.
2. Check `df_rate_limited_total{scope}` (`data-events` / `control` / `lifecycle` /
   `ws-connect`) — which surface is being hammered, and whether the limiter is already
   absorbing it.

## What is always on (no action needed)

- **Per-IP signup windows (RL-1):** `SIGNUP_RATE_LIMIT_PER_HOUR` (default 5) /
  `SIGNUP_RATE_LIMIT_PER_DAY` (default 20), keyed by the trusted-edge `Fly-Client-IP`
  (never client-controlled `X-Forwarded-For`; `backend/identity/infra/rate_limit.py`).
- **Disposable-email denylist (SEC-ACC-9):** signups against a vendored disposable-domain
  list are rejected (`backend/identity/infra/disposable_email.py`); the list refreshes via
  Dependabot.
- **No-state-oracle auth (A-3):** every auth failure is the single closed slug, so a flood
  learns nothing.

## Throttle / tighten (escalating)

1. **Lower the signup windows** (operator config) if the flood is registration-driven:
   reduce `SIGNUP_RATE_LIMIT_PER_HOUR` / `_PER_DAY` via `fly secrets set` (or app config) →
   rolling restart of `web`. Legitimate signup is rare per-IP, so this bites bots first.
2. **Enable/confirm per-key rate limits.** Production runs with `DF_RATE_LIMIT_ENABLED=1`
   (`backend/config/rate_limit_middleware.py`); if a wave is concentrated on one key, the
   per-key token buckets (data-events 600/min, control 120/min, lifecycle 30/min,
   ws-connect 10/min) already return `429 rate-limited` + `Retry-After` and bump
   `df_rate_limited_total{scope}`. Confirm it is enabled; the limiter fails **open** if
   Redis degrades, so check Redis health if you expected throttling and see none.

## Disposable-email / captcha controls

3. **Refresh the disposable denylist** if attackers use a fresh disposable domain not yet on
   the list — merge the Dependabot refresh PR (the override path in `disposable_email.py`),
   or add the domain to the override.
4. **Flip captcha on (SEC-ACC-10) — the documented first response for sustained signup/auth
   abuse.** Set `SIGNUP_CAPTCHA_PROVIDER=turnstile` + `TURNSTILE_SECRET_KEY=<secret>` via
   `fly secrets set` → rolling restart `web`. Signup and password-reset-request then require
   a valid Cloudflare Turnstile token (`backend/identity/infra/captcha.py`, server-side
   siteverify, fail-closed). Default is `none`; this is a config flip, not a deploy.

## Block

5. **Block the source at the edge** for a concentrated single-source flood (IP / ASN /
   geo) at the Fly edge / WAF — cheaper than absorbing it in-app. Use `Fly-Client-IP`
   (the trusted edge value) to identify; do not trust `X-Forwarded-For`.
6. For a flood on a single **compromised key**, revoke it →
   [security-leaked-key.md](security-leaked-key.md).

## Escalation

- If availability is degrading for legitimate tenants (`ApiAvailabilityBurn` paging),
  escalate to sev-1, page the on-call, and prioritize the edge block over in-app throttles.
- If the wave correlates with a credential leak (auth failures against valid usernames),
  treat as a credential-stuffing incident: force-rotate affected sessions and consider a
  `JWT_SIGNING_KEY` break-glass rotation ([security-key-rotation.md](security-key-rotation.md))
  if platform-wide session invalidation is warranted.

## Verification

- `df_auth_failures_total[5m]` back under the `AuthFailureSpike` threshold (50);
  `AuthFailureSpike` resolves.
- `df_rate_limited_total{scope}` flattens (the abusive source is throttled/blocked).
- Legitimate signup/login works (with captcha if flipped on); `ApiAvailabilityBurn` clear.
- Per-key/per-IP isolation held: no legitimate tenant's counters moved by the attacker
  (buckets are per-key isolated, INV-OBS-3).
- Record what was tightened (windows, captcha, edge block) so it can be relaxed once the
  wave subsides.
