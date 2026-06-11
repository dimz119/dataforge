# ADR-0016 — Frontend: Vite + React + TS SPA, TanStack Query, generated client, no Redux

**Deliverable:** D17

The DataForge console is a Vite-built React 19 + TypeScript single-page application with no server-side rendering, using TanStack Query as the only server-state store (no Redux or any global client store), a TypeScript client generated from the OpenAPI CI artifact, a dedicated WebSocket hook layer with reconnect/backoff and client-side sampling for high-TPS tails, in-memory access tokens with rotating refresh cookies, and Tailwind CSS with headless (Radix) components. This warranted an ADR because the frontend architecture decisions that hurt later — state-management topology, contract coupling, and how live data reaches the render tree — are exactly the ones that look interchangeable on day one and cost a rewrite at the first 100-TPS live tail or the first silent FE/BE drift incident.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** all console code from the Phase 1 shell through every page group (Phase 7+); the FE side of the contract pipeline (ADR-0001, ADR-0014); console token handling (with ADR-0011)

## Context

The forces:

- **React + TypeScript is the mandated stack**; the console must deliver seven page groups (auth, dashboard, workspaces, scenarios, stream control, API keys, monitoring) per the PRD, with the entire core flow usable by a non-curl human as the Phase 7 exit criterion.
- **The console is an authenticated tool, not a website:** everything sits behind login; there is no SEO surface, no anonymous content, no first-paint marketing concern. Its hottest views (live tail, stream stats) are push- and poll-driven.
- **The live tail is a render-safety problem:** the monitoring page must show a WS tail at 100+ TPS without freezing (Phase 7 exit criterion), while WS itself is an at-most-once tail channel (ADR-0013) — the UI layer must absorb burst rates the DOM cannot.
- **Contract drift is the chronic SPA disease:** the API evolves every phase; a hand-maintained client diverges silently. ADR-0001/0014 already make OpenAPI a CI artifact; the frontend must consume it such that drift fails the build.
- **Token security:** the console holds a human's JWT (ADR-0011). XSS-exfiltratable storage (localStorage) is the standard mistake; the design must make a stolen-token blast radius small and time-bounded.

## Decision

1. **SPA, no SSR.** Vite 6 builds a static bundle served by the `web` process group; no Node render tier exists in the deployment topology (ADR-0015) and none is added. Accepted costs — blank-shell first paint behind auth (mitigated by an inline boot splash) and route-level code splitting as the bundle lever — are detailed in [../02-architecture/frontend-architecture.md](../02-architecture/frontend-architecture.md) §1.2.
2. **TanStack Query 5 is the only server-state store; there is no global client store.** Every piece of state has exactly one home: server state in the Query cache; live push state in the `useStreamTail` ring buffer (deliberately outside the Query cache); navigation state in the URL (the active workspace is `/w/:slug` — no `currentWorkspace` store to desynchronize); auth state in the `TokenManager` module; ephemeral UI state in component state. Routing is React Router 7 in library/data-router mode, whose nested layouts map 1:1 onto the auth/workspace/admin guard hierarchy.
3. **Generated OpenAPI client.** `openapi-typescript` generates types and `openapi-fetch` provides the typed transport from the drf-spectacular artifact (ADR-0014); the drift gate regenerates `schema.gen.ts` in CI and fails on any diff. Only `shared/api/client.ts` may call `fetch` (lint-enforced), so the auth middleware owns the transport exactly once.
4. **Dedicated WS hook layer.** A `TailSocket` wrapper plus `useStreamTail` hook own the WebSocket lifecycle: versioned-subprotocol connect, exponential reconnect with backoff and jitter, resume-from-cursor handoff on reconnect (ADR-0013), explicit rendering of drop-notice frames, and **client-side sampling** — above a configurable render budget the hook samples events into a bounded ring buffer and reports the sampling rate, so a 1,000-TPS tail renders a truthful subset instead of freezing the tab. Tail rows render through a virtualized list (TanStack Virtual).
5. **Token handling:** the access token lives in a module variable only (never localStorage/sessionStorage/JS-readable cookies); the rotating refresh token is an `HttpOnly; Secure; SameSite=Strict` cookie scoped to the refresh path. Single-flight refresh (proactive at < 30 s remaining; reactive on 401 exactly once), multi-tab coordination via `BroadcastChannel`, and logout clearing the Query cache are specified in frontend-architecture §6. The console never holds API keys beyond the reveal-once dialog (ADR-0011).
6. **UI stack:** Tailwind CSS 4 with design tokens in `@theme`, Radix UI primitives for accessible headless behavior, react-hook-form + zod mapped onto RFC 9457 field errors (ADR-0014). No component framework, no charting library in the MVP bundle.

The state taxonomy that replaces a global store (full table in frontend-architecture §1.3):

| State kind | Single home |
|---|---|
| Server state (workspaces, streams, stats, keys, schemas, answer key) | TanStack Query cache |
| Live push state (tail events, connection status) | `useStreamTail` ring buffer — outside the Query cache |
| Navigation state (active workspace `/w/:slug`, tabs, filters) | The URL |
| Auth state (access token, current user) | `TokenManager` module + `['session']` query |
| Ephemeral UI state (dialogs, drags, drafts) | Component state / Radix internals |

## Alternatives considered

- **Next.js (or any SSR/RSC framework).** Rejected: SSR's payoffs — SEO, anonymous first paint, edge personalization — do not exist behind a login wall, while its cost is a Node render tier in a deployment topology that deliberately has none (process groups are `web`/`ws`/`worker`/`runner`, ADR-0015) plus a second data-fetching idiom competing with TanStack Query. Marketing pages, if ever needed, are a separate static site.
- **Redux (or Zustand/MobX) as a global store** — the conventional default this ADR's title explicitly rejects. Rejected: ~90% of console state is server state, which a global store would duplicate into a second, hand-invalidated cache — the precise source of stale-data bugs TanStack Query exists to eliminate (staleness, retries, invalidation, optimistic updates are library concerns, not reducer code). The state taxonomy (frontend-architecture §1.3) leaves no orphan state that would justify the store.
- **TanStack Router instead of React Router.** Considered for its type-safe route params; rejected: React Router 7's data-router mode covers the needs (nested layouts ≙ guard hierarchy, lazy route modules) with a larger ecosystem and team familiarity, and route-param type safety is recovered at the API boundary by the generated client types. Not a one-way door — the route table is isolated in `app/router.tsx`.
- **Hand-written API client over `axios`/`fetch`.** Rejected: silent contract drift is the failure mode ADR-0001/0014 are built to prevent; hand-written types rot in exactly the ways the drift gate cannot see. Generated hooks (e.g. Orval) were also rejected — generating *types* but writing query options by hand keeps cache keys and invalidation explicit (frontend-architecture §5.2).
- **Tokens in `localStorage` (or refresh token in JS-readable storage).** Rejected: any XSS becomes durable credential theft. In-memory access + HttpOnly rotating refresh bounds an XSS to the access-token lifetime within the open tab, complemented by CSP and lint-banned `dangerouslySetInnerHTML`.
- **A full component framework (MUI, Ant Design).** Rejected: imposed visual language and bundle weight against a console whose hardest UI problems (live tail virtualization, status-driven controls) are custom anyway; Tailwind + Radix gives accessibility primitives without the lock-in.

## Consequences

### Positive

- FE/BE drift is a compile/CI error in the causing PR; the generated client plus `strict: true` makes the OpenAPI artifact the single contract surface (ADR-0014).
- Render safety at high TPS is a designed property — sampling + ring buffer + virtualization — not an emergent failure; the tail degrades honestly (sampling rate and drop notices are displayed, mirroring the channel's at-most-once semantics).
- No store-vs-cache coherence bugs by construction; URL-as-state makes deep links and refreshes correct for free.

### Negative

- Blank-shell first paint and a session-bootstrap round-trip (`POST /auth/refresh`) on every full reload — accepted for an authenticated tool; mitigated by the boot splash and suspense-gated guards.
- The sampled tail is observational, not complete: any completeness claim must route to REST replay or the answer key (ADR-0013, ADR-0017); the UI labels the tail accordingly.
- Page reloads drop the in-memory access token by design, making the refresh cookie path availability-critical for UX; its failure modes are specified in frontend-architecture §6.2–6.3.

### Follow-ups

- [../02-architecture/frontend-architecture.md](../02-architecture/frontend-architecture.md): the binding implementation spec — feature folders, route table, query-key and invalidation conventions, WS frame contract, sampling parameters, error/empty-state conventions, testing strategy.
- [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md): WS subprotocol and frame shapes the hook layer implements; problem-details catalog the forms map onto.
- Phase 7: Playwright E2E of the full core flow against the compose stack; tail-at-100+-TPS non-freeze exit criterion.
