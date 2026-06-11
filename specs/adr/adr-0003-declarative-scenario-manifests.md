# ADR-0003 — Scenarios are declarative versioned manifests interpreted by a generic runtime

**Deliverable:** D17

A DataForge scenario is a versioned YAML/JSON manifest — data, not code — validated against a published JSON Schema plus semantic and dry-run checks, and interpreted by one generic runtime. This is a one-way door: the manifest grammar is the contract every scenario, every workspace override, every pinned stream, and the future AI-generation path depend on; if scenarios were code, retrofitting them into data later would mean rewriting the behavior engine and abandoning every shipped scenario.

- **Status:** Accepted — review-blocking (one-way door)
- **Date:** 2026-06-10
- **Decides for:** the scenario plugin architecture (D6), the behavior engine's input contract, the catalog, the AI slot-in; grammar frozen Phase 0, validator Phase 3

## Context

The forces:

- **The requirement names the mechanism's properties:** "unlimited future scenarios … added WITHOUT modifying core. Each scenario defines: entities, relationships, event types, business rules, event flows, data generators," and the explicit hint that "AI-generated scenarios from a prompt … must slot in without major refactor — implies scenarios should be expressible as declarative manifests interpreted by a generic runtime, not only hand-written code."
- **The AI path makes manifests attacker-controlled input.** The panel's gap analysis found all three proposals under-specified safety: "an LLM emits a manifest that passes JSON Schema validation" is necessary but not sufficient. A schema-valid manifest can still encode a 40-state near-absorbing loop, 100k-row pools across 50 entities, or pathological generators — a tenant-level DoS vector against the shared runtime. Validation must therefore cover resource bounds, probability sums, reachability/termination, and a generator allowlist, with an explicit threat model.
- **Determinism and grading** (ADR-0008, ADR-0017) require that a stream's behavior be a pure function of an immutable, hashable artifact. Data versions and pins cleanly; code does not.
- **The panel gap on pinning:** none of the three proposals defined what happens to running streams when a manifest or workspace overrides change. The pinning semantics must be part of this decision.

## Decision

1. **A scenario is a versioned manifest** with the section set fixed in [../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §2: `metadata`, `entities` (attribute generators drawn from a closed, named vocabulary), `relationships`, `event_types` (payload mappings, `partition_by`), `state_machines` (probabilistic transitions, dwell distributions, guards/preconditions, effects), `cdc`, `intensity`, `seeding`, `chaos_defaults`, discriminated by `manifest_schema: v0`. The Manifest v0 JSON Schema is frozen at Phase 0 (§9.1) and becomes a CI artifact at Phase 3.
2. **Every manifest passes one three-layer validation pipeline** — builtin, human-authored, or LLM-emitted; there is no trust gradient in validation (§8): hardened parse (size ≤ 512 KiB, depth ≤ 12, no YAML anchors) → JSON Schema → semantic checks (referential closure MAN-V1xx; per-state probability sums MAN-V201; reachability, escape-edge, and expected-steps termination checks MAN-V204/V205/V207; resource bounds B-01…B-17; generator allowlist) → a seeded, budgeted dry run on the actual runtime (MAN-D6xx). Only a fully passing version can be published (INV-CAT-2).
3. **One generic runtime.** The behavior engine interprets any valid manifest via a compiled immutable `ManifestIR`; there is no per-scenario interpreter, subclass, or branch (P-2). Expressiveness gaps are fixed by additive grammar growth, never runtime special-casing.
4. **Hooks: value generation only, and never in the reference scenario.** Platform-registered, allowlisted Python `hook` generators may compute single attribute values. The grammars for transitions, guards, dwell, effects, and payload structure have no hook slot — control-flow hooks are unrepresentable, not merely forbidden (P-3). Hooks are rejected in `workspace`-visibility manifests (MAN-V404), and the shipped e-commerce manifest must contain zero hooks, enforced by a permanent CI assertion (P-4) — the forcing function that keeps the DSL honest.
5. **Published versions are immutable; running streams pin.** Manifest updates create new semver versions, never mutate published ones (INV-CAT-1). A stream copies its `(manifest_version, merged configuration)` at start and keeps it for life; instance re-pinning and override edits affect only streams started afterwards; only desired run-state, target TPS, chaos toggles within pinned bounds, and scheduled schema upgrades are live-mutable (PIN-1…PIN-5, INV-CAT-4, INV-STR-5) — closing the panel's pinning gap.
6. **AI-generated scenarios are manifests that validate — zero core change.** The slot-in contract (§12) is the catalog API plus machine-actionable validation errors (`{code, JSON Pointer path, message, bound, actual}`) as the LLM repair-loop input, workspace visibility as the capability ceiling, and validator rate quotas. The prompt→manifest service itself is post-MVP (Phase 12+); everything it needs ships in MVP and is exercised continuously by builtin registration and override validation.

What each validation layer is for (full pipeline in the D6 spec §8):

| Layer | Question it answers | Representative checks |
|---|---|---|
| Parse hardening | Is the document safe to even read? | size ≤ 512 KiB, depth ≤ 12, no anchors/aliases |
| L1 — JSON Schema | Is the shape grammatical? | structure, patterns, count caps expressible structurally |
| L2 — semantic | Is the content coherent and bounded? | referential closure, probability sums, reachability/termination, B-01…B-17, generator allowlist |
| L3 — dry run | Is the *realized* behavior affordable? | budgeted seeded execution: traversal caps, payload sizes, ≥ 1,000 events/s/shard floor |

## Alternatives considered

- **Scenario-as-code plugins** — a Python class per scenario implementing a runtime interface (the natural Django ecosystem pattern; maximal expressiveness). Rejected: the AI requirement would mean executing untrusted, LLM-authored Python in the platform's processes — sandboxing untrusted Python is a losing security game, and no resource-bound story comparable to B-01…B-17 exists for arbitrary code; "no core change per scenario" erodes as each plugin grows bespoke branches; versioning/pinning/hashing code for determinism (INV-G-4) is far weaker than hashing canonical JSON; and grading requires trusting every scenario author's implementation of invariants the manifest grammar guarantees structurally.
- **Hybrid: declarative skeleton + per-scenario code for "the hard parts"** (business rules, event flows as code). Rejected: the escape hatch becomes the norm under deadline pressure, and the AI path inherits the code problem anyway. DataForge's narrow concession is the value-generation hook with no control-flow reach — and even that is banned from the reference scenario so vocabulary gaps are fixed in the grammar.
- **Manifests with JSON-Schema-only validation** — effectively what all three panel proposals specified. Rejected per the gap analysis: schema conformance cannot express probability sums, termination, cross-section referential closure, or realized cost; a schema-valid manifest remains a DoS vector. The three-layer pipeline with concrete bounds and a dry run is the substantive difference between "parseable" and "safe to accept."
- **Register the full 8-entity e-commerce manifest immediately (P2/P3) vs. a subset first (P1).** Resolved as a synthesis: the **full** manifest is written at Phase 0 as the spec worked example ([../04-engines/scenarios/ecommerce.md](../04-engines/scenarios/ecommerce.md)) — forcing the DSL to prove expressiveness on paper, per P2's risk note — but only the subset manifest is registered and executed in Phases 3–7, with the full manifest landing at Phase 8. Runtime slices stay small and reviewable; the grammar is still stress-tested before freeze.

## Consequences

### Positive

- Adding a scenario — human or AI — is a data ingestion event; `grep -r ecommerce backend/ --include='*.py'` matching anything but the builtin YAML path is a CI failure from Phase 3.
- Determinism, pinning, and gradable exercises follow from immutable, hashable artifacts (PIN-1: `(manifest_version, seed, merged-config sha256)` is the determinism unit).
- The threat model is enforceable: four rings (static bounds → allowlist → dry run → runtime quotas/leases), each independently testable (§13, T-1…T-9).

### Negative

- **The DSL is a ceiling.** Scenario authors cannot express what the grammar cannot say; growth is additive and governed (§9.3), which is deliberately slower than writing code. Accepted: the ceiling is the security and determinism boundary.
- Generic interpretation costs CPU versus hand-rolled generators; bounded by the dry-run floor MAN-D604 (≥ 1,000 events/s per shard on the runner instance class).
- The validator is a significant subsystem with its own attack surface (T-6) and maintenance load (per-generator param catalogs, bound recalibration as the reference scenario grows).

### Follow-ups

- [../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) owns grammar, bounds, pipeline, pinning, threat model; [../06-quality/security-architecture.md](../06-quality/security-architecture.md) owns the platform-wide untrusted-manifest view and validator rate limits.
- Phase 3 ships the validator (L1+L2) and catalog; Phase 4 ships L3 with the behavior engine and retroactively re-validates the builtins.
- Content moderation for the prompt→manifest service is a recorded launch requirement of that post-MVP service (threat T-8).
