# ADR-0010 — In-house schema registry: Postgres + JSON Schema, Confluent-compatible subjects

**Deliverable:** D17

Event payload schemas are JSON Schema documents stored in Postgres, keyed by Confluent-compatible subjects with monotonically versioned, immutable entries and enforced additive compatibility; Confluent Schema Registry is not deployed in the MVP, but subject naming is fixed to its convention now. This is a one-way door because every envelope ever emitted stamps a `schema_ref {subject, version}` into the ledger, the buffer, golden fixtures, and user pipelines: subject naming and version semantics are unchangeable once the first event exists, and choosing the Confluent-compatible form costs nothing today while any other choice would make the Phase-12 external-Kafka mirroring a permanent translation layer.

- **Status:** Accepted — review-blocking (one-way door)
- **Date:** 2026-06-10
- **Decides for:** the schema registry (D9); `schema_ref` resolution on every envelope; the schema-drift chaos mode's field source; Phases 3, 10, 12

## Context

The forces:

- **The requirement is a teaching feature, not plumbing:** "versioned event schemas (v1/v2/v3 additive field example), every event carries schema metadata; supports schema evolution exercises." The registry must be queryable mid-exercise (E5: pipelines adapt to drift using the registry API) and drive the Phase-10 mid-stream upgrade demo.
- **Drift chaos needs a coherent field source.** Injecting arbitrary made-up fields teaches nothing checkable; the panel decision is that `schema_drift` may only inject fields from a *registered next version* — which requires a registry that knows what the next version is (INV-REG-5).
- **JSON is the user-facing surface:** students `curl` the cursor API and read events by eye; the envelope is JSON with no binary wire format in MVP (ADR-0004 rejected Avro/Protobuf for the contract), so a Confluent SR would govern a serialization that does not exist yet.
- **Registration must be transactional with manifest publication:** schemas are *derived* from manifests (R-DER-1…R-DER-5 in [../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §5.2) in the same database transaction as the publish — natural for in-house Postgres rows, awkward across a remote service.
- **Resolved disagreement:** only panel position P2 fixed Confluent-compatible subject naming now. Adopted: it is free today and a one-way door once external consumers exist.

## Decision

1. **The registry is the `registry` Django app over Postgres** (domain model §2.4): Subject aggregate keyed `{scenario_slug}.{event_type}` for business events and `{scenario_slug}.cdc.{entity_type}` for CDC row images (INV-REG-1); the event-type name grammar makes subject collisions structurally impossible (R-EVT-1, R-DER-5).
2. **Versions are monotonic integers (1, 2, 3 …) per subject, immutable once registered** (INV-REG-2) — Confluent's version model exactly, so Phase-12 mirroring maps 1:1. Manifest semver is a separate, parallel axis: a manifest minor version registers a next integer version only for subjects whose derived schema changed (R-DER-4).
3. **Compatibility mode is `BACKWARD_ADDITIVE`, enforced at registration** (INV-REG-3): a new version may only add optional fields — never remove a field, change a type, or add a required field. The check runs during manifest validation so a non-additive payload change fails *publication* with MAN-V501 (fail at the manifest, not at the registry).
4. **Derived schemas are closed** — every field `required`, `additionalProperties: false` (R-DER-3) — deliberately, so `corrupted_values`, `nulls`, and `schema_drift` are detectable *violations* of the pinned version rather than tolerated noise.
5. **Every envelope stamps `schema_ref {subject, version}`** resolving to a registered entry at emission time; an unresolvable ref is a generation bug, never delivered (INV-REG-4, ADR-0004).
6. **Read surface:** `GET /api/v1/schemas/{subject}/versions` and version-history/diff APIs from Phase 3 ([../05-interfaces/api-specification.md](../05-interfaces/api-specification.md)); the console registry browser ships Phase 10 with the v2/v3 evolution exercise and scheduled mid-stream upgrades.
7. **Drift linkage (the coherence rule):** `schema_drift` injects only fields defined in a registered version newer than the stream's pinned version (INV-REG-5), and never into CDC `before` images (R-CDC-6). The registry is therefore a chaos-engine dependency from Phase 9's first commit.
8. **Confluent SR is not deployed in MVP.** At Phase 12 (external Kafka channel), subjects and versions mirror mechanically into a managed SR for native-client ecosystems; the in-house registry remains the system of record ([../04-engines/schema-registry.md](../04-engines/schema-registry.md) owns the mirroring contract).

The platform's three independent version axes, so they are never conflated:

| Artifact | Version scheme | Evolution rule | Owner |
|---|---|---|---|
| Event envelope | `major.minor` string, frozen `1.x` | Additive-only minor bumps (EV-1…EV-7) | ADR-0004, event-model §8 |
| Scenario manifest | Semver, human intent | Patch = docs; minor = additive; major = anything (immutable once published) | ADR-0003, scenario-plugin-architecture §9.2 |
| Registry subject | Monotonic integer per subject | `BACKWARD_ADDITIVE` at registration (INV-REG-3) | this ADR, schema-registry.md |

R-DER-4 is the only coupling: a manifest minor bump registers next integer versions for exactly the subjects whose derived schemas changed.

## Alternatives considered

- **Deploy Confluent Schema Registry (or Karapace) from Phase 3.** Rejected: a JVM service added to the MVP footprint to govern a binary wire format the MVP does not have; its compatibility checking is Avro-oriented while DataForge's payloads are JSON Schema; and transactional derive-and-register inside manifest publication (R-DER-4) would become a cross-service consistency problem. The legitimate future need — native Kafka client ecosystems at Phase 12 — is answered by fixing the subject convention now so mirroring is mechanical, not by carrying the service for nine phases unused.
- **No registry — schemas implied by the pinned manifest version.** Rejected: schema lineage must be queryable per subject independent of manifests (consumers resolve `schema_ref` without ever seeing a manifest); drift chaos would have no registered next version to draw from, making INV-REG-5 unstatable; the v1/v2/v3 evolution exercise and the Phase-10 registry browser would have no substrate.
- **Semver-versioned registry entries.** Rejected: Confluent versions are integers; a semver registry forces a translation layer in every mirrored subject forever. The design keeps semver where humans need intent (manifests) and integers where the wire needs lineage (subjects), with R-DER-4 as the bridge.
- **Confluent's full compatibility-mode matrix** (BACKWARD, FORWARD, FULL, transitive variants) from day one. Rejected for MVP: one strict mode is the teaching story — additive evolution mirrors the envelope policy (EV-1) and is the only mode the e-commerce exercise needs. The `CompatibilityMode` value object exists on the Subject (domain model §2.4) so further modes are an additive post-MVP change.
- **Inline schemas in the envelope** (each event carries its full JSON Schema). Rejected: kilobytes per event against the B-12 size bound, and it teaches the wrong lesson — production pipelines resolve schema *pointers* against a registry; `schema_ref` resolution is itself part of exercise E5.

## Consequences

### Positive

- The registry is a teaching instrument from Phase 3: schema browsing, version diffs, drift detection, and the mid-stream v1→v2 upgrade are all real registry operations, not simulations.
- Drift chaos is coherent and gradable: every injected field resolves to a registered version, so "detect and adapt using the registry" has a correct answer.
- Zero additional infrastructure; registration is transactional with manifest publication, so a published scenario's subjects are never missing or stale.

### Negative

- We own compatibility-check correctness — a checker bug could admit a breaking version; mitigated by property tests over the additive-compat algorithm ([../06-quality/testing-strategy.md](../06-quality/testing-strategy.md)) and by INV-REG-2 immutability bounding the blast radius to one bad version.
- The read API is DataForge's own `/api/v1` shape, not Confluent's wire-compatible REST API — native SR client libraries cannot point at it until Phase-12 mirroring; accepted, since MVP consumers are REST/WS users.
- `BACKWARD_ADDITIVE` means a subject's lineage can never show removals or renames; "breaking change" exercises must be modeled as new subjects under a major manifest version. Accepted: the additive discipline *is* the curriculum, and it matches the frozen envelope policy.

### Follow-ups

- [../04-engines/schema-registry.md](../04-engines/schema-registry.md) owns registration mechanics, the compatibility algorithm, the v1/v2/v3 walkthrough, and the Phase-12 mirroring contract.
- Phase 3 ships the registry app, derivation from the subset manifest, and the read API; Phase 10 ships the browser UI, diff API, and scheduled mid-stream upgrades; Phase 12 executes mirroring alongside the external Kafka channel (ADR-0015 trigger).
