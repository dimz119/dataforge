"""DataForge runner — the data-plane process (backend-architecture §8).

A long-lived Django-context asyncio program (not a Django *app*): the only code
path that emits events (ADR-0006). Public surface, by module:

* :mod:`runner.leases` — the Redis lease authority + fencing-token kernel (§8.2):
  ``LeaseManager``, ``Lease``, ``ShardKey``, ``lease_key``, ``LEASE_TTL_MS``.
* :mod:`runner.fencing` — fencing enforcement (§8.2): ``FencingError``,
  ``enforce_conditional_write``, ``is_fresh_token``, ``fence_key``.
* :mod:`runner.publisher` — internal Kafka producer (§8.3 step 8 / §8.6):
  ``EventPublisher``, ``DELIVERY_TOPIC``, ``build_kafka_producer``.
* :mod:`runner.checkpoint_store` — the §8.2 fenced conditional checkpoint write +
  failover restore: ``CheckpointStore``, ``RestoredCheckpoint``.
* :mod:`runner.shard_worker` — the §8.3 normative reconciliation tick:
  ``ShardWorker``.
* :mod:`runner.supervisor` — the asyncio supervisor (§8.1): ``Supervisor``,
  ``AdmissionControl``.

These modules are imported by path (several import Django at module load, so this
package ``__init__`` stays import-light to keep ``import runner`` cheap and the
argument-parsing smoke tests Django-free).
"""

from __future__ import annotations

__all__: list[str] = []
