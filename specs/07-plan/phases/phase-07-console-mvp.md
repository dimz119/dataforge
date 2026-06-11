# Phase 7 — Console MVP (frontend)

**Deliverable:** D18 (phase doc)

This phase makes the entire core product loop — account → workspace → scenario → API key → stream → live events → pause/resume → stop — usable by a non-curl human in a browser. Everything the console renders already exists as API surface from Phases 2–6; this phase adds no backend behavior beyond what the generated-client pipeline requires. The console implements all seven page groups exactly as specified in [../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) §9, on the stack pinned by ADR-0016 (Vite + React + TS SPA, TanStack Query, generated OpenAPI client, no Redux — see [../../adr/README.md](../../adr/README.md)).

## Goal

> The entire core flow usable by a non-curl human in a browser.

## Dependencies

- **Phase 2** — auth, workspace, membership, API-key endpoints; audit API (the `ActivityList` reads it).
- **Phase 3** — scenario catalog + instance/overlay APIs with MAN-V\* error codes (the `OverlayErrorMap` maps them).
- **Phase 5** — stream lifecycle APIs, cursor REST pull.
- **Phase 6** — pause/resume, dynamic TPS, WS tail with versioned subprotocol + resume-from-cursor, stats API (the console's Monitoring pages are thin clients of these).
- **OpenAPI CI artifact** (ADR-0014, in place since Phase 1) — input to the generated TypeScript client.
- Specs: [../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) (normative for every component), [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md) (problem-type catalog, WS frames/close codes).

## Scope

1. **App shell**: routing map + guards (`RequireAuth`, `RequireVerified`, `RequireAdmin`), workspace switcher, lazy route chunks, skeleton loading conventions (frontend-architecture §2–3, §10).
2. **Generated client pipeline live in CI**: `gen:api` from the schema artifact; `gen:api:check` drift job — FE/BE contract drift fails the build (ADR-0014/0016).
3. **Auth pages**: signup, email verify, login, forgot/reset password, refresh single-flight, in-memory access token.
4. **Dashboard**: workspace summary, per-stream stats cards (5 s poll), `GettingStartedPanel`.
5. **Workspace management**: create/switch, members + roles (INV-TEN-3 guards in UI), activity list, danger zone with cascade enumeration.
6. **Scenario management**: catalog grid, scenario detail, instance config page — probability sliders clamped to `override` bounds, dwell editors, catalog sizes, intensity-curve editor, CDC toggles, chaos-defaults section (writes instance defaults only; the live chaos panel is Phase 9), `OverlayErrorMap`.
7. **API keys**: keys table, create dialog with scopes, reveal-once dialog (plaintext never persisted, INV-TEN-4), quickstart curl snippet.
8. **Stream control**: create page (seed, TPS, virtual-clock fields accepting only `1×`/`live` until Phase 8), control panel with the normative button-enablement matrix, log-scale TPS slider, pin summary.
9. **Monitoring**: overview table, stream monitor, `LiveTail` on the `useStreamTail` hook layer — reconnect/backoff, resume-from-cursor, client-side sampling, virtualized list, drop-notice rows.
10. **Error conventions**: RFC 9457 problem-type switch (`validation-error`, `cursor-expired`, `rate-limited`, …), empty states with CTAs, form pointer mapping.
11. **E2E suites**: `auth.spec.ts`, `core-loop.spec.ts` (PR smoke); `keys.spec.ts`, `stream-control.spec.ts`, `live-tail.spec.ts` (nightly), per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §12.

## Non-goals

- **No chaos panel, no answer-key panel** — Phase 9 (the stream detail tab bar is `control`-only until then).
- **No registry browser UI** — Phase 10 (registry stays API-only).
- **No quota meters or self-serve plans** — Phase 11 (`WorkspaceSummaryCard` shows usage numbers without limit bars).
- **No `channels` feature** (external sinks) — Phase 12; the SideNav slot is reserved, not rendered.
- **No virtual-clock multipliers ≠ 1 or backfill mode in the UI** — fields and API contract exist now; values unlock in Phase 8.
- **No backend changes** except additive OpenAPI annotations needed for client generation; any endpoint behavior change in this phase's diff is a review reject.

## Tasks

Each task is one reviewable PR; IDs are referenced from commit messages.

- [ ] **P7-01** — App shell: theme tokens, `shared/ui` primitives (Button/Input/DataTable/StatusBadge/JsonViewer/CopyField/CodeSnippet), route table + guards, error boundary, toast provider.
- [ ] **P7-02** — Generated client pipeline: `gen:api`, `schema.gen.ts`, typed fetch wrapper, problem-details parser (`ApiError`), CI drift check (`gen:api:check`).
- [ ] **P7-03** — Auth: token manager (memory access + rotating refresh, single-flight), login/signup/verify/reset pages, session bootstrap, multi-tab logout.
- [ ] **P7-04** — TanStack Query setup: client defaults, query-key conventions, invalidation matrix, polling rules (2 s convergence poll, 5 s stats poll).
- [ ] **P7-05** — Dashboard: summary card, stream stats cards with sparkline, `GettingStartedPanel`.
- [ ] **P7-06** — Workspaces: switcher, create form, members table with INV-TEN-3 disabled states, activity list, danger zone.
- [ ] **P7-07** — API keys: table, create dialog, reveal-once dialog (dialog-local state only), quickstart snippet.
- [ ] **P7-08** — Scenarios: catalog grid + scenario detail + create instance.
- [ ] **P7-09** — Instance config page: probability sliders, dwell editors, catalog-size inputs, intensity-curve editor, CDC toggles, chaos defaults, `OverlayErrorMap` (JSON-Pointer → control mapping), `config_revision` footer.
- [ ] **P7-10** — Stream create + control panel: button-enablement matrix, TPS slider (debounced, optimistic), pin summary, danger zone.
- [ ] **P7-11** — WS hook layer: `TailSocket`, `useStreamTail`, reconnect/backoff schedule, 4401 reauth + cursor-expired `error`-frame handling, `resume_ack`/REST gap-fill (frontend-architecture §7.4), client-side sampling + 4 Hz batching; `FakeTailSocket` test harness.
- [ ] **P7-12** — Monitoring: overview table, stream monitor page, `LiveTail` with virtualization, type filter, drop-notice/cursor-expired notice rows, per-type counters.
- [ ] **P7-13** — Error/empty/loading conventions wired across all pages; a11y pass (jsx-a11y lint, focus management, reduced-motion).
- [ ] **P7-14** — E2E: `auth.spec.ts` + `core-loop.spec.ts` in the PR lane against compose (Mailpit token fetch); nightly `keys/stream-control/live-tail` specs; `@axe-core/playwright` checks.
- [ ] **P7-15** — Performance budgets in CI: entry ≤ 250 KB gzip, route chunks ≤ 150 KB, tail main-thread task < 50 ms at 1,000 TPS ingest (sampled input).

## Demo script

Run from the repo root; the demo mirrors `e2e/core-loop.spec.ts` step for step (seed `4242` = `SEED_E2E`, so the data matches the PRD §2.2 instructor journey and the docs):

```bash
docker compose up -d --wait          # pg, redis, kafka, api, ws, worker, runner, buffer-writer, web + dev-only mailpit
curl -fsS localhost:8000/readyz      # all probes green
open http://localhost:5173
```

1. **Sign up** with a fresh email → open Mailpit (`http://localhost:8025`), click the verification link → land on the console.
2. **Create workspace** `demo-ws` → dashboard shows the `GettingStartedPanel`.
3. **Scenarios** → E-Commerce → create instance with defaults; drag one probability slider out of its allowed range → save → the `MAN-V201` error highlights the exact slider group.  Fix and save.
4. **API keys** → create key with `events:read`, `streams:read`, `streams:write` → reveal-once dialog → copy → close → confirm the table shows only `df_live_…last4`.
5. **Streams** → create (instance, seed `4242`, 10 TPS) → Start → `StatusBadge`: `starting` → `running` in ≤ 60 s.
6. **Monitoring** → live tail shows events; expand an `order_placed` row and confirm `payload.user_id` matches an earlier `user_registered` (the PRD activation moment).
7. **TPS slider** 10 → 200 → observed TPS follows within 10 s; tail stays responsive at 100+ TPS with the `SamplingBadge` active.
8. **Pause** → badge `paused`, tail stops appending; **Resume** → appending continues. **Stop** → `stopped`.
9. Headless proof: `cd frontend && npx playwright test e2e/core-loop.spec.ts`.

## Exit criteria

Binding criteria with proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 7 rows):

| # | Criterion | Proof | Lane |
|---|---|---|---|
| 1 | A new user completes account → workspace → scenario → key → start → watch live events → pause/resume → stop **entirely in the UI**, within the 15-minute time-to-first-event budget of [../../01-product/prd.md](../../01-product/prd.md) §2.1/§8 | E2E `core-loop.spec.ts` passes in CI against the compose stack | PR smoke |
| 2 | Live tail at 100+ TPS does not freeze: no main-thread task > 200 ms, counters monotonic, type filter works | E2E `live-tail.spec.ts` (Playwright tracing assertion) | nightly |
| 3 | All lifecycle UI states render correctly: `starting/running/pausing/paused/stopping/stopped/failed` badges + button matrix; `paused_quota` renders (state reachable via test fixture even though enforcement lands in Phase 11) | E2E `stream-control.spec.ts` | nightly |
| 4 | Reveal-once holds in the UI: plaintext key appears in exactly one dialog, never in the DOM afterwards; revoked key rejected within 1 s | E2E `keys.spec.ts` + OPS-6 | nightly |
| 5 | Generated-client lockstep: CI fails on any FE/BE schema drift | CON §8.1 client-lockstep gate | PR (permanent) |
| 6 | Coverage gates: `shared/api` + `shared/ws` ≥ 90 % line, features ≥ 70 %; bundle budgets hold | vitest coverage + size check in CI | PR |
| 7 | No accessibility regressions on one representative page per page group | `@axe-core/playwright` assertions | nightly |

Phase review starts from this table; all seven rows green ⇒ Phase 8 may begin.
