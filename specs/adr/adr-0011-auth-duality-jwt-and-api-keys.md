# ADR-0011 — Auth duality: JWT for the console, hashed workspace-scoped API keys for the data plane

**Deliverable:** D17

Humans authenticate to the console with SimpleJWT short-lived access tokens and rotating refresh tokens; machines authenticate to the data plane with opaque, workspace-scoped, scope-limited API keys stored only as SHA-256 hashes and revocable within one second via a Redis revocation cache. This warranted an ADR because the two principal classes have opposite requirements — session ergonomics versus headless longevity, statelessness versus instant revocation — and a single mechanism stretched across both inevitably compromises one; the decision also fixes the account-lifecycle and abuse-control scope the panel found missing from all three proposals.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the security architecture (D14); authentication on every API surface; the API-key lifecycle (domain model §5); Phase 2

## Context

The forces:

- **Two principal classes, opposite needs.** Humans: browser sessions, password login, reset flows, short-lived credentials whose theft window should be minutes. Machines: long-lived headless credentials used at high request rates from notebooks, cron jobs, and student laptops — credentials that get pasted into config files and committed to GitHub, so leakage is the expected failure mode and *fast revocation* is the requirement (Phase 2 exit criterion: revoked key rejected within 1 s).
- **SimpleJWT is mandated** in the stack, and the stateless-API NFR wants request authentication without a database hit — but pure statelessness and instant revocation are in direct tension; the design must say where each wins.
- **The classroom flow shapes key UX:** students issue their own keys inside the instructor's workspace (PRD §2.2), so key creation is member-level, low-ceremony, reveal-once; keys are the credential that crosses the internet to the data plane, making them tenancy-critical (INV-TEN-4).
- **The panel gap this ADR fixes in scope:** none of the three proposals specified email verification, password reset, account deletion, or signup rate-limiting — required for a public, free-tier classroom SaaS where free accounts drive compute cost.

## Decision

1. **Console (humans): SimpleJWT.** Short-lived access token + rotating refresh token; the SPA holds the access token in memory only, with refresh rotation (ADR-0016). JWTs authenticate the control-plane console surface: account, workspace, membership, key management, scenario configuration, and stream control in the UI.
2. **Data plane (machines): opaque API keys** in the format `df_<env>_<prefix>_<secret>`. Stored as SHA-256 hash + `prefix` + `last4` only; plaintext returned exactly once in the creation response (INV-TEN-4). Each key is scoped to **exactly one workspace** with a scope subset of `events:read`, `streams:read`, `streams:write`, `schemas:read`, `answer_key:read` (the last grantable only by a workspace admin); optional `expires_at`; `last_used_at` tracked write-behind.
3. **Revocation is near-instant:** revocable by the key's creator or any workspace admin; a Redis revocation cache makes the effect ≤ 1 s without a per-request database hit; workspace deletion cascades revocation (INV-TEN-6); `revoked` and `expired` are terminal — reissue, never reactivate (domain model §5).
4. **Surface assignment:** data-plane endpoints — event consumption (REST cursor, WS), stream lifecycle via `streams:write`, schema reads, answer-key reads — authenticate with API keys; console/account mutations authenticate with JWT. The endpoint-by-endpoint matrix is owned by [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md). Failure semantics: unknown/revoked/expired key → **401**; valid credential with insufficient scope or a foreign workspace → **403/404** per the cross-tenant disclosure policy in [../06-quality/security-architecture.md](../06-quality/security-architecture.md).
5. **Account lifecycle and abuse controls are part of this decision's scope** (closing the panel gap): email verification (single-use token, TTL 24 h; unverified users cannot create workspaces, accept invitations, or issue keys — INV-ID-2), password reset (single-use token, TTL 1 h), account deletion (blocked while the user is a sole workspace admin, INV-ID-4/INV-TEN-3), and signup rate-limiting/anti-abuse controls — mechanics, hashing parameters, and thresholds owned by [../06-quality/security-architecture.md](../06-quality/security-architecture.md).

The duality at a glance:

| Aspect | JWT (console) | API key (data plane) |
|---|---|---|
| Principal | Human, browser session | Machine, headless consumer |
| Credential form | Signed claims token, parseable | Opaque `df_<env>_<prefix>_<secret>`, no decode surface |
| Lifetime | Access: minutes; refresh: rotating | Indefinite unless `expires_at` set |
| At-rest storage | Not stored server-side | SHA-256 hash + `prefix` + `last4` only |
| Scope | The user's memberships and roles | One workspace × explicit scope set |
| Revocation | Refresh rotation/expiry | ≤ 1 s via Redis revocation cache |
| Theft blast radius | Minutes-long console session | One workspace × scopes, until revoked |

## Alternatives considered

- **Long-lived JWTs as API keys** (one mechanism everywhere). Rejected: a stateless JWT cannot be revoked without a denylist — at which point the revocation cache has been built anyway, but the credential is now a *parseable, claims-bearing* token whose contents tempt clients into trusting unverified claims, and whose signing-key rotation invalidates every issued key at once. An opaque random secret has no decode surface, no claims to drift, and hashes cleanly at rest.
- **API keys for everything, no JWT.** Rejected: SimpleJWT is mandated; humans need session semantics — password login, refresh rotation, short browser-exposure windows, reset flows. A long-lived key held by a browser combines the theft surface of a cookie with the lifetime of a machine credential.
- **OAuth2 client-credentials flow for machines.** Rejected for MVP: there are no third-party integrations; an authorization server is substantial scope serving no current consumer; and the opaque-key model is exactly what learners meet at warehouse and API vendors. Nothing in the seam (an `Authorization` header convention) precludes adding OAuth2 post-MVP.
- **Reversible encryption of stored keys** (re-showable secrets). Rejected: a database exfiltration plus a KMS misstep re-exposes every key in the platform; hash + reveal-once is strictly safer, and the recovery path — revoke and reissue in seconds — is cheap by design.
- **Per-user keys valid across the user's workspaces.** Rejected: the key must be the *tenant boundary's* credential — a cross-workspace key would make every data-plane authorization check membership-dependent and turn one leaked student key into a multi-workspace incident. Workspace-scoped keys keep INV-TEN-4 a single equality check, and the classroom flow (per-student keys within one workspace) falls out naturally via member-level creation.

## Consequences

### Positive

- Blast-radius separation: a leaked key exposes one workspace × its scopes and dies within a second of revocation; a leaked access token expires in minutes and never touches the data plane's scope model.
- The data plane authenticates with one Redis-cached hash lookup — no JWT parsing, no session state — preserving the stateless-API NFR where throughput matters.
- The cross-tenant attack suite (Phase 2 exit criterion) has a crisp target: every endpoint probed with foreign-workspace credentials of *both* types must return 403/404.

### Negative

- Two authentication paths through middleware means double the negative-test surface; the attack suite and the OpenAPI security schemes must cover both permanently.
- The Redis revocation cache is a correctness dependency: its failure semantics (fail-closed vs fail-open, cache rebuild) must be explicit — owned by [../06-quality/security-architecture.md](../06-quality/security-architecture.md), with the 1 s revocation bound as the test.
- Reveal-once causes lost-key friction for students; mitigated by cheap reissue and `prefix`/`last4` identification in the console, and accepted as the cost of hash-only storage.

### Follow-ups

- [../06-quality/security-architecture.md](../06-quality/security-architecture.md) owns JWT lifetimes/rotation, key-hashing parameters, revocation-cache failure semantics, abuse-control thresholds, and the verification/reset/deletion flows.
- [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md) owns header conventions, the per-endpoint credential matrix, and the 401/403/404 problem types (ADR-0014).
- Phase 2 implements both paths with the revocation (≤ 1 s) and cross-tenant attack-suite exit criteria; Phase 11 adds per-key rate limits tied to plan quotas (PRD §7).
