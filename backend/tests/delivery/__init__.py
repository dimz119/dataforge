"""Delivery-plane tests (delivery-channels §3-§4).

Covers the cross-channel :class:`~delivery.domain.channel.DeliveryChannel`
conformance suite (§3.7), the buffer-writer ``rest_buffer`` sink (§4: strip → COPY →
offset-after-commit, ``buffer_seq`` monotonicity), and the sink host
poll/batch/commit harness (§3.5).

The Postgres-backed ``buffer_seq`` / COPY / RLS assertions live in
``tests/delivery/test_postgres`` (run in the publish-path + isolation lanes; the
SQLite unit lane exercises the same logic over the migration's plain-table
fallback). The kill/replay OPS conformance row (§3.7) is compose-only (needs Kafka
+ a sink host SIGKILL) — see the Phase-5 CI note.
"""
