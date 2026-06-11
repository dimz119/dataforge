# ADR-0017 — Instructor ground-truth answer-key API

**Deliverable:** D17

Every chaos injection and the canonical clean sequence are queryable by workspace admins through dedicated answer-key endpoints and a console panel — injection types, counts, affected `event_id`s, field-level mutations, configured and realized timing — while ground truth never appears in any delivered event payload (stripped at the delivery boundary per ADR-0004). This warranted an ADR because it is the decision that turns the chaos engine from a noise generator into a gradable teaching instrument: without it, the product's stated differentiator is unverifiable — by instructors grading labs, and equally by DataForge's own exit criteria.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the only external surface for ground truth (Phase 9 API + console panel); the gating model for that surface; what the chaos engine must persist for it (with ADR-0009); every exercise's grading contract (PRD §5)

## Context

The forces:

- **The instructor persona's hardest job** is objective grading: "know precisely which events were injected as chaos so submissions can be scored against ground truth" (PRD §2.2). Today that instructor hand-builds CSV fixtures *because* hand-built fixtures are the only data whose truth they know. A chaos stream without an answer key reproduces the problem it claims to solve.
- **Students need self-verification:** "verify their pipeline produced the right answer without an instructor present" (PRD §2.1) — which the same surface serves through workspace-admin-mediated or scope-granted access.
- **The architecture already pays for the substrate.** The staged pipeline (ADR-0009) persists a clean canonical ledger, and INV-CHA-4 requires every injection to be recorded in an InjectionRecord *before* its effect is delivered. The answer key is a read API over data that exists for correctness reasons — the marginal cost is endpoints and gating, not a new engine.
- **The tension to resolve:** ground truth must be simultaneously *fully queryable* (grading) and *completely absent from the delivered stream* (otherwise `SELECT … WHERE is_duplicate = false` replaces the dedup exercise). These are separable surfaces only if the strip boundary is absolute.
- **Panel provenance:** only P3 proposed this (as its ADR-0015); adopted in synthesis because it makes the chaos exit criteria objectively verifiable ("answer-key counts exactly match injections end-to-end", Phase 9) and directly serves a named primary persona. The PRD's exercise-completion success metric (≥ 30% of preset streams query the answer key within 7 days, PRD §8) is measurable only because this surface exists.

## Decision

1. **Dedicated endpoints under the stream resource** (shapes owned by [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md); content contract fixed in [../03-domain/event-model.md](../03-domain/event-model.md) §7.3): `GET /api/v1/streams/{stream_id}/answer-key/injections` (cursor-paginated per ADR-0014, filterable by `mode`, time window, and `event_id`) returning per-injection records — `injection_id`, mode, affected `event_id`(s), `sequence_no`, field-level mutation details with original values, configured vs realized timing; plus canonical-truth reads (per-window aggregates such as orders/day and per-partition counts for E7/E8, and canonical-sequence access for byte-comparable grading of E3) served from the ground-truth ledger.
2. **Substrate = ledger + InjectionRecords, nothing else.** The answer key reads the ground-truth ledger (clean canonical sequence, ADR-0009) joined with append-only InjectionRecords. Because recording precedes delivery (INV-CHA-4) and chaos is seed-deterministic (INV-CHA-2), answer-key counts match delivered chaos exactly, to the event — including discarded/flushed late re-emissions, whose outcomes are recorded on their injection records.
3. **Gating:** accessible to workspace `admin`s via the console (JWT, `IsWorkspaceAdmin`) and to machines via API keys carrying the `answer_key:read` scope — grantable **only by a workspace admin**, so a student member cannot self-grant it (domain model §5; [../06-quality/security-architecture.md](../06-quality/security-architecture.md) §4). Every answer-key access writes an audit entry (domain model §2.10 minimum audited set). Workspace-scoped like everything else: INV-TEN-1 applies, and the cross-tenant attack suite probes these endpoints from Phase 9.
4. **The strip boundary makes the duality safe** (ADR-0004; event-model §5): internal `_df` ground-truth labels are stripped at sink ingestion on every channel (SB-2), the `_df` prefix is reserved at every nesting level (SB-1), a permanent CI scan of every channel's delivered output enforces it (SB-3), and the answer-key API is the **only** external surface for ground truth (SB-4, INV-DEL-2). Delivered streams and the answer key never mix.
5. **Console Answer Key panel** (Phase 9, stream detail tab — [../02-architecture/frontend-architecture.md](../02-architecture/frontend-architecture.md) §9.5): per-mode injection summaries and counts, drill-down to `event_id` lists, and export of the injection report instructors score against (PRD §2.2 journey). Exercise presets (PRD §5) pair each lab with its expected answer-key queries.

The two surfaces, kept disjoint:

| Surface | Carries | Audience | Guarded by |
|---|---|---|---|
| Delivered stream (every channel) | Post-chaos envelopes, `_df` stripped — no ground truth, ever | Any consumer with `events:read` | Strip boundary SB-1…SB-3 + permanent CI scan (INV-DEL-2) |
| Answer-key API + console panel | InjectionRecords + canonical ledger reads | Workspace admins; keys with admin-granted `answer_key:read` | Role/scope gating + audit-on-access + cross-tenant suite (SB-4) |

## Alternatives considered

- **No answer key — chaos as configuration-only realism** (P1/P2's implicit position; only P3 proposed the surface). Rejected: an instructor cannot grade against unknown truth, so the differentiator collapses to "noisy data", indistinguishable in value from a faker script with a random delay. It would also leave Phase 9's exit criteria unverifiable in principle — "configured 5% duplicates realized" is only provable against recorded injections.
- **Ground-truth markers in delivered payloads** (e.g. an `is_duplicate` or `injected: true` flag, perhaps "only for instructors' streams"). Rejected outright: any in-band marker destroys exercise validity — students filter on it, and per-audience payload variation would break the invariant that every channel delivers the identical envelope (event-model §6). ADR-0004 froze the opposite rule: internal labels ride internally and are stripped at the boundary.
- **Post-hoc derivation — diff the delivered stream against the ledger when asked.** Rejected: it cannot distinguish a chaos duplicate from an at-least-once redelivery (both repeat `event_id`s; only the chaos one is an injection), cannot see suppressed (`missing`) events' *reasons*, prices grading as an O(stream) batch job at query time, and inverts INV-CHA-4 — truth would be reconstructed rather than recorded. InjectionRecords written before delivery make the answer key exact by construction.
- **Export-file-only (downloadable injection log, no API).** Rejected as the primary surface: programmatic grading (scoring 60 student submissions) and the console panel both need queryable, filterable access; the export the instructor journey needs is trivially built *on* the API, not instead of it.
- **Answer key visible to all workspace members.** Rejected: in classroom workspaces students are `member`s and must not see the answers mid-lab. Admin role + admin-grantable scope puts disclosure under instructor control; the residual risk (an admin sharing the key out of band) is a classroom-policy matter, not a platform control (TM-8).

## Consequences

### Positive

- Chaos becomes gradable: every exercise in the PRD catalog (E1–E8) carries an exact, queryable grading contract, and "no other tool can grade a deduplication pipeline against known truth" becomes a defensible product claim.
- Verification is reflexive: the same surface instructors grade with is what the Phase 9 exit criteria and the statistical test suite measure against — product feature and test oracle are one artifact.
- Determinism (ADR-0008) plus recorded injections means a re-run lab with the same seed has the same answer key — semester-over-semester reproducibility (PRD §2.2).

### Negative

- InjectionRecord persistence is a write-amplification cost on chaos-heavy streams (one record per injection, plus per-field mutation detail); bounded by chaos-rate caps and accounted in the buffer/ledger capacity arithmetic ([../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md)).
- The answer key is a sensitive read surface that must be permanently defended: scope gating, audit-on-access, and the SB-3 CI scan are standing obligations, and the cross-tenant suite gains endpoints to probe.
- Answer-key availability is coupled to ledger retention (7-day rolling default): grading windows for long-running labs must fit retention, or instructors export the injection report — stated in the console panel and exercise docs.

### Follow-ups

- [../04-engines/chaos-engine.md](../04-engines/chaos-engine.md): InjectionRecord field schema per mode, recording transaction semantics (INV-CHA-4), late-buffer outcome recording, and the exercise-preset definitions.
- [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md): answer-key endpoint catalog, query parameters, response shapes, scope enforcement.
- [../03-domain/database-schema.md](../03-domain/database-schema.md): InjectionRecord DDL, indexes for mode/time/event_id queries, RLS.
- [../06-quality/testing-strategy.md](../06-quality/testing-strategy.md) / Phase 9: end-to-end count-match tests (answer key ⇔ delivered chaos), determinism replay of injections, cross-tenant probes of the new endpoints.
