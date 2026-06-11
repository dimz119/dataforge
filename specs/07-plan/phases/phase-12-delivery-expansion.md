# Phase 12 — Delivery Expansion: External Kafka + Webhooks (post-MVP)

**Deliverable:** D18 (phase doc)

This phase ships the first post-MVP delivery channels — DataForge-hosted per-workspace Kafka topics with SASL/ACL credentials, and HMAC-signed webhooks — and in doing so **proves the DeliveryChannel seam**: both channels are new consumer adapters behind the interface frozen in [../../04-engines/delivery-channels.md](../../04-engines/delivery-channels.md) (ADR-0005), joining the existing cross-channel test harness with zero new assertion logic and **zero changes to behavior, chaos, or runner code**. It also executes the pre-committed managed-Kafka migration (ADR-0015 trigger clause "the external Kafka delivery channel ships", [../../02-architecture/deployment-architecture.md](../../02-architecture/deployment-architecture.md) §4) as an entry task, before any external topic is provisioned. The consumption-model boundary is unchanged: internal Kafka remains invisible; external topics are a separate, credentialed surface.

## Goal

> Prove the `DeliveryChannel` seam by shipping the Phase-2 product channels with zero generation-side changes.

## Dependencies

- **Phase 11 (MVP GA)** — production topology, observability, quotas (channel provisioning is quota-gated: hosted topics/webhooks are a Classroom add-on / Pro feature per [../../01-product/prd.md](../../01-product/prd.md) §7).
- **Phase 5** — `DeliveryChannel` interface and the internal Kafka backbone (the seam under proof).
- **Frozen contracts**: per-channel guarantees table in [../../03-domain/event-model.md](../../03-domain/event-model.md) §6 (external Kafka: at-least-once, per-partition FIFO by `partition_key`, 7-day topic retention; webhooks: at-least-once with exponential backoff + DLQ, idempotency key = `event_id`); strip boundary SB-2/SB-3 (every sink calls `strip_internal` at ingest); registry mirroring procedure ([../../04-engines/schema-registry.md](../../04-engines/schema-registry.md) §13).

## Scope

1. **Managed-Kafka migration (entry task)**: move the internal backbone from the single-broker Fly KRaft VM to the selected managed provider per the deployment-architecture §4 procedure. Broker endpoints are config (`KAFKA_BOOTSTRAP_SERVERS` + new SASL secrets); the swap is infra-only and is itself part of the seam proof.
2. **External Kafka sink**: a `kafka_external` SinkBinding consuming post-chaos internal topics and producing to per-workspace external topics on the managed cluster; SASL/SCRAM credential provisioning (created/revoked via API, secret shown once — same reveal-once discipline as API keys, INV-TEN-4 analogue); ACLs restricting each principal to its workspace's topic prefix; topic naming, partitioning, retention, and tombstone mapping per delivery-channels.md.
3. **Webhook sink**: a `webhook` SinkBinding POSTing event batches to a user endpoint with HMAC-SHA256 signature headers, exponential-backoff retries, dead-letter handling after retry exhaustion, per-delivery logs, and DLQ redelivery on request.
4. **Channel configuration API + console UI**: SinkBinding CRUD under `/api/v1/streams/{id}/sinks` (shapes owned by [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md)); the reserved `channels` console feature ([../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) §13) for provisioning, credential reveal, endpoint + secret management, and delivery-log viewing.
5. **Cross-channel contract tests**: XCH-4 adapters join the existing harness — identical envelope/content guarantees across REST/WS/Kafka/webhook; the strip-boundary scan extends to both new channels the day they ship (SB-3).
6. **Registry mirroring**: mirror subjects/versions to the managed provider's schema registry per the mechanical procedure of schema-registry.md §13 (subject names have been Confluent-compatible since Phase 3).
7. **Connection guides**: Spark Structured Streaming, Flink, and Kafka Connect against the hosted topics; webhook receiver reference implementation (HMAC verification + idempotent processing). These supersede the MVP "bridge" guides for users on the new channels; the bridge exercise remains documented for REST-only plans.
8. **Future-sink groundwork**: the S3/Iceberg/CDC-export contract (file/commit semantics, exactly-once-per-file) stays frozen in delivery-channels.md; this phase adds no implementation but verifies by review that the SinkBinding model and harness admit it without interface change.

## Non-goals

- **No generation-side changes**: zero diff in `backend/generation/`, `backend/chaos/`, runner/lease/checkpoint code, the envelope, or the manifest contract — this is an exit criterion, not just a guideline.
- **No S3/Iceberg/CDC export implementation** — post-Phase-12, through the same seam.
- **No user-defined Kafka clusters as destinations** — the channel is DataForge-*hosted* topics; pushing to customer-owned brokers is a future sink decision.
- **No AI-manifest generation service** — independent post-MVP track (scenario-plugin §12).
- **No new chaos modes, schema features, or console redesign** — only the `channels` feature is added.

## Tasks

- [ ] **P12-01** — Execute the managed-Kafka migration runbook: provision managed cluster, dual-write window, sink offset cutover, decommission the Fly broker VM; SLO-2 monitored throughout; config/secrets-only diff.
- [ ] **P12-02** — SinkBinding model + channel config API: CRUD, per-plan quota gating, audit entries (`delivery.sink.created`/`.revoked`), TEN auto-enrollment of the new routes.
- [ ] **P12-03** — External Kafka adapter: consumer-group per binding, `strip_internal` at ingest, produce keyed by `partition_key` to the workspace topic; per-partition FIFO preserved; lag metrics.
- [ ] **P12-04** — Credential provisioning: SASL/SCRAM principal per binding, ACLs scoped to the workspace topic prefix, reveal-once secret, revocation; negative-path tests (foreign principal → authorization failure).
- [ ] **P12-05** — Webhook adapter: batch POST with `DF-Webhook-Signature` (HMAC-SHA256 over body, per-binding secret), retry schedule with exponential backoff + jitter, DLQ after exhaustion, delivery logs + redelivery endpoint.
- [ ] **P12-06** — Console `channels` feature: sink list/create/revoke, credential reveal-once dialog, webhook endpoint + secret management, delivery-log table with DLQ redeliver action.
- [ ] **P12-07** — XCH-4: Kafka + webhook adapters added to the cross-channel harness (content equality per `event_id`, per-partition FIFO assertion, HMAC validation, retry-idempotency) — zero new assertion logic (testing-strategy TP-6).
- [ ] **P12-08** — Strip-boundary + tenancy extension: SB-3 scan over both new channels; TEN §7.5(P12) probes (foreign SASL credentials, cross-workspace webhook config probes).
- [ ] **P12-09** — Registry mirroring job to the managed schema registry; mirror-consistency check in CI.
- [ ] **P12-10** — Connection guides (Spark, Flink, Kafka Connect) + reference webhook receiver; each guide executed end-to-end against a live stream before publication.
- [ ] **P12-11** — Seam-proof audit: GUARD import-lint (channel SDKs importable only from delivery adapter modules) + scripted diff-confinement check (P12 exit row 4) wired into the phase-gate review.

## Demo script

```bash
# 1. Provision the channel (console or API) for a running stream:
curl -s -X POST localhost:8000/api/v1/streams/$SID/sinks -H "X-API-Key: $KEY" \
  -d '{"sink_type":"kafka_external"}'        # → topic name + SASL username + reveal-once password
# 2. Consume with plain Kafka tooling — own credentials succeed:
kafka-console-consumer --bootstrap-server $MANAGED_BROKER \
  --topic df.$WORKSPACE_ID.$SID --group demo \
  --consumer-property security.protocol=SASL_SSL \
  --consumer-property sasl.mechanism=SCRAM-SHA-256 \
  --consumer-property "sasl.jaas.config=...username=\"$SASL_USER\" password=\"$SASL_PASS\";" \
  | jq .event_type                            # only own-workspace events; _df never present
# 3. Foreign credentials fail (negative isolation proof):
#    same command with another workspace's principal → TopicAuthorizationException
# 4. Webhook: register the reference receiver, watch signed deliveries arrive:
curl -s -X POST .../sinks -d '{"sink_type":"webhook","url":"https://receiver.local/hook"}'
python guides/webhook-receiver/main.py        # verifies DF-Webhook-Signature, logs event_ids
# 5. Kill the receiver for 2 min → retries with backoff; keep it down → entries land in the DLQ;
#    bring it back → redeliver from DLQ via the console; receiver dedups on event_id (idempotency)
# 6. Seam proof, live: show the phase diff is adapters + config only
git diff --stat v1.0.0-ga..HEAD -- backend/generation backend/chaos backend/streams/runner   # → empty
```

## Exit criteria

| # | Criterion | Proof ([../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md)) | Lane |
|---|---|---|---|
| 1 | `kafka-console-consumer` with issued credentials receives **only own-workspace events**; the same consumption attempted with foreign-workspace credentials fails authorization (negative test) | TEN §7.5(P12) | PR (permanent) |
| 2 | Sample webhook receiver verifies HMAC on every delivery and survives retry and DLQ scenarios (receiver down → backoff retries → DLQ → redelivery, idempotent on `event_id`) | XCH-4 webhook adapter cases | merge |
| 3 | Cross-channel contract tests pass on all four channels: identical delivered envelope and content per `event_id` across REST/WS/Kafka/webhook; per-partition FIFO on Kafka; `_df` absent everywhere | XCH-1..4 full + SB-3 strip scan | merge |
| 4 | **Seam proof — the diff is confined to delivery adapters + config.** Concretely: `git diff v1.0.0-ga..phase-12` touches only `backend/delivery/`, the sink/credential API + console `channels` feature, `infra/` config-and-secrets, guides, and tests; **zero changes** under `backend/generation/`, `backend/chaos/`, runner/lease/checkpoint modules, the envelope schema artifact, or the manifest JSON Schema. Enforced by the GUARD import-lint (only delivery adapters import channel SDKs) plus a scripted path-confinement check on the phase diff, and confirmed in phase review | GUARD §8.4 + P12-11 audit | PR + gate run |
| 5 | XCH-4 adapters added **zero new assertion logic** — the harness assertions of Phases 6–9 run unmodified against the new channels (TP-6) | harness diff review | gate run |
| 6 | Managed-Kafka migration completed via config/secrets only, with SLO-2 within budget across the cutover window | migration runbook record + SLO dashboard | gate run |
| 7 | All three connection guides (Spark, Flink, Kafka Connect) executed successfully end-to-end against a live stream | guide walkthrough drill | gate run |
