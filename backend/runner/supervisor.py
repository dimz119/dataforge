"""Runner supervisor — the asyncio data-plane program (backend-architecture §8.1).

Replaces the Phase-1 heartbeat stub with the real supervisor: it owns the Redis
:class:`~runner.leases.LeaseManager`, runs the claimable scan + lease acquisition
under **admission control**, spawns one :class:`~runner.shard_worker.ShardWorker`
asyncio task per held generation lease, heartbeats every 5 s (cancelling workers
whose lease was lost, §8.2), and serves the internal aiohttp health listener on
:8081 — now with a ``/readyz`` that reports real lease/reconcile state.

Roles (§8.1): ``generation`` runs shard workers; ``sinks`` runs the §8.6 consumer
groups (built in the sink-host area, not here); ``all`` runs both. This module
owns the generation supervision + lease loop; the sink consumers are started
alongside it by the entrypoint when the role includes them.

Admission control (§8.1): a runner has an events-per-second budget
(``RUNNER_EPS_BUDGET``, default 5,000) and a shard cap (``RUNNER_SHARD_CAPACITY``,
default 8). It refuses a new lease when adding the shard's ``target_tps`` would
push ``Σ target_tps`` of held shards over budget, or when the shard count is at the
cap — TPS-weighted placement without a central scheduler. A refused candidate is
simply not claimed this scan; another runner (or this one, later) picks it up.

The control plane writes desired state; this supervisor reconciles toward it by
*which shards it leases*, and each shard worker reconciles *what it emits*. No
command bus (ADR-0006).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web
from django.conf import settings

from observation.infra import metrics
from runner.leases import LeaseManager, ShardKey
from runner.publisher import DELIVERY_TOPIC, EventPublisher, build_kafka_producer
from runner.shard_worker import ShardWorker
from streams.application import desired_state

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from runner.leases import Lease
    from runner.publisher import KafkaProducer
    from streams.application.desired_state import DesiredState

logger = structlog.get_logger("dataforge.runner")

HEALTH_PORT = 8081
HEARTBEAT_INTERVAL_S = 5.0  # §8.2 renew-all cadence
CLAIMABLE_SCAN_INTERVAL_S = 2.0  # §8.2 claimable scan cadence

__all__ = ["AdmissionControl", "Supervisor"]


@dataclass
class AdmissionControl:
    """TPS-weighted lease admission (§8.1). One per generation supervisor.

    Tracks the held shards' aggregate ``target_tps`` and count so :meth:`admits`
    can refuse a candidate that would breach the EPS budget or the shard cap before
    a lease is acquired. ``register`` / ``release`` keep the running totals as
    workers start and stop.
    """

    eps_budget: int
    shard_capacity: int
    _held_tps: dict[ShardKey, int] = field(default_factory=dict)

    @property
    def held_shards(self) -> int:
        return len(self._held_tps)

    @property
    def held_tps(self) -> int:
        return sum(self._held_tps.values())

    def admits(self, shard: ShardKey, target_tps: int) -> bool:
        """Would leasing ``shard`` at ``target_tps`` stay within budget + cap?"""
        if shard in self._held_tps:
            return True  # already held; re-admitting is free
        if self.held_shards + 1 > self.shard_capacity:
            return False
        return self.held_tps + max(0, target_tps) <= self.eps_budget

    def register(self, shard: ShardKey, target_tps: int) -> None:
        self._held_tps[shard] = max(0, target_tps)

    def release(self, shard: ShardKey) -> None:
        self._held_tps.pop(shard, None)


class Supervisor:
    """The asyncio data-plane supervisor (§8.1).

    Owns the lease manager, the admission control, the live shard-worker task map,
    the heartbeat + claimable-scan loops, and the :8081 health listener. ``run`` is
    the process body launched by ``python -m runner``.
    """

    def __init__(
        self,
        role: str,
        *,
        redis: Redis | None = None,
        producer: KafkaProducer | None = None,
        runner_id: str | None = None,
    ) -> None:
        self.role = role
        self.runner_id = runner_id or f"runner-{int(time.time() * 1000)}"
        self._redis = redis
        self._producer = producer
        self._leases: LeaseManager | None = None
        self._admission = AdmissionControl(
            eps_budget=settings.RUNNER_EPS_BUDGET,
            shard_capacity=settings.RUNNER_SHARD_CAPACITY,
        )
        # shard → (worker, task). The live generation workers under supervision.
        self._workers: dict[ShardKey, tuple[ShardWorker, asyncio.Task[None]]] = {}
        self._stop = asyncio.Event()
        self._last_beat = time.monotonic()
        self._last_scan = 0.0
        self._ready = False
        # The §8.6 sink hosts (buffer-writer + ws-pusher) each run in a daemon thread
        # when the role includes sinks — each is a blocking Kafka consumer loop, not an
        # asyncio task, and holds no Redis lease (the broker's group coordinator does
        # that work). Both consume df.delivery.events.v1 in separate consumer groups so
        # one sink's lag never stalls the other (§3.5 isolation).
        self._sink_hosts: list[Any] = []
        self._sink_threads: list[Any] = []

    @property
    def runs_generation(self) -> bool:
        return self.role in ("generation", "all")

    @property
    def runs_sinks(self) -> bool:
        return self.role in ("sinks", "all")

    # -- health listener ---------------------------------------------------------

    async def healthz(self, request: web.Request) -> web.Response:
        """Liveness: the heartbeat advanced within 2x its interval (observability §6.1)."""
        fresh = (time.monotonic() - self._last_beat) <= 2 * HEARTBEAT_INTERVAL_S
        return web.json_response(
            {"status": "ok" if fresh else "wedged", "service": settings.DF_SERVICE},
            status=200 if fresh else 503,
        )

    async def readyz(self, request: web.Request) -> web.Response:
        """Readiness: lease/reconcile state — held shards + live workers (§8.1)."""
        ready = self._ready
        return web.json_response(
            {
                "status": "ready" if ready else "starting",
                "components": {
                    "redis": self._redis is not None,
                    "kafka": self._producer is not None,
                },
                "lease": {
                    "runner_id": self.runner_id,
                    "held_shards": self._admission.held_shards,
                    "held_tps": self._admission.held_tps,
                    "live_workers": len(self._workers),
                },
                "gating": [] if ready else ["lease_loop"],
                "release": settings.RELEASE,
            },
            status=200 if ready else 503,
        )

    # -- claimable scan + admission-controlled acquisition (§8.1/§8.2) ------------

    async def _scan_and_acquire(self) -> None:
        """Claimable scan (§8.2, 2 s): claim admissible shards, refuse over-budget.

        Reads the batched desired-state set (one Postgres read), maps it to shard
        keys, filters to those with no live lease (``claimable_scan``), and for each
        admissible one acquires the lease + spawns a worker. A shard the admission
        control refuses is left for another runner (or a later scan once headroom
        frees up).
        """
        assert self._leases is not None
        desired_rows = await asyncio.to_thread(desired_state.claimable_desired_states)
        by_shard = self._shard_index(desired_rows)
        claimable = await self._leases.claimable_scan(list(by_shard))
        for shard in claimable:
            desired = by_shard[shard]
            if not self._admission.admits(shard, desired.target_tps):
                logger.info(
                    "admission.refused",
                    stream_id=shard.stream_id,
                    shard_id=shard.shard_id,
                    target_tps=desired.target_tps,
                    held_tps=self._admission.held_tps,
                    held_shards=self._admission.held_shards,
                )
                continue
            await self._acquire_and_spawn(shard, desired)

    def _shard_index(
        self, rows: list[DesiredState]
    ) -> dict[ShardKey, DesiredState]:
        """Fan a stream's desired state over its shards → ``{ShardKey: desired}``.

        MVP is one shard (``shard_id = 0``); the loop is shard-count-general so the
        Phase-11 multi-shard split is a config change, not a topology change.
        """  # Phase 11 (multi-shard fan-out)
        index: dict[ShardKey, DesiredState] = {}
        for row in rows:
            for shard_id in range(max(1, row.shard_count)):
                index[ShardKey.of(row.stream_id, shard_id)] = row
        return index

    async def _acquire_and_spawn(self, shard: ShardKey, desired: DesiredState) -> None:
        """Acquire the lease (INCR fence + SET NX) and spawn the worker task."""
        assert self._leases is not None
        lease = await self._leases.acquire(shard.stream_id, shard.shard_id)
        if lease is None:
            return  # NX-blocked: another runner won the race this scan
        self._admission.register(shard, desired.target_tps)
        self._spawn_worker(shard, lease)
        # df_runner_lease_takeovers_total{reason}: the fencing token is a never-reset
        # per-shard INCR, so token == 1 is the very first acquire (first start) and a
        # token > 1 means a prior holder existed — this acquire is a FAILOVER takeover
        # (the lease key had expired and the claimable scan surfaced it). The metric
        # backs the RunnerLeaseTakeoverSpike page (rate[10m] > 3). M-3: ``reason`` is
        # bounded (failover/first_start), never a stream/shard id.
        reason = "failover" if lease.fencing_token > 1 else "first_start"
        metrics.runner_lease_takeovers_total.labels(reason=reason).inc()
        self._publish_lease_gauges()
        logger.info(
            "lease.acquired",
            stream_id=shard.stream_id,
            shard_id=shard.shard_id,
            fencing_token=lease.fencing_token,
        )

    def _publish_lease_gauges(self) -> None:
        """Refresh ``df_runner_active_leases`` from the live held set.

        The count of shard leases this runner holds — a 4-shard stream contributes 4.
        Called after every acquire/release/heartbeat-loss so the gauge tracks the held
        set exactly. (``df_runner_streams_running`` is owned by the shard worker, which
        inc/decs it per running worker — kept separate to avoid a double-writer race.)
        """
        metrics.runner_active_leases.set(len(self._workers))

    def _spawn_worker(self, shard: ShardKey, lease: Lease) -> None:
        assert self._redis is not None and self._producer is not None
        publisher = EventPublisher(self._producer, topic=DELIVERY_TOPIC)
        worker = ShardWorker(lease=lease, publisher=publisher, redis=self._redis)
        task = asyncio.create_task(
            self._supervise_worker(shard, worker), name=f"shard:{shard.stream_id}:{shard.shard_id}"
        )
        self._workers[shard] = (worker, task)

    async def _supervise_worker(self, shard: ShardKey, worker: ShardWorker) -> None:
        """Run a worker; on any exit drop it from the held set + release the lease.

        A clean return is a finalized stop (T10, the worker already released). A
        :class:`~runner.fencing.FencingError` / cancellation is a takeover or lost
        lease — release defensively (compare-owner, so a re-acquired lease is safe).
        """
        try:
            await worker.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "shard.worker_exit",
                stream_id=shard.stream_id,
                shard_id=shard.shard_id,
                error=str(exc),
            )
        finally:
            self._admission.release(shard)
            self._workers.pop(shard, None)
            self._publish_lease_gauges()
            if self._leases is not None:
                with contextlib.suppress(Exception):
                    await self._leases.release(shard.stream_id, shard.shard_id)

    # -- heartbeat (§8.2, 5 s renew-all; cancel lost-lease workers) ---------------

    async def _heartbeat_once(self) -> None:
        """Renew all held leases; cancel workers whose lease was lost (§8.2)."""
        self._last_beat = time.monotonic()
        if self._leases is None:
            return
        lost = await self._leases.heartbeat()
        for shard in lost:
            await self._cancel_worker(shard, reason="lease_lost")

    async def _cancel_worker(self, shard: ShardKey, *, reason: str) -> None:
        """Cancel a shard's worker task before its next pipeline step (§8.2)."""
        entry = self._workers.get(shard)
        if entry is None:
            return
        _worker, task = entry
        logger.warning(
            "shard.cancel",
            stream_id=shard.stream_id,
            shard_id=shard.shard_id,
            reason=reason,
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # -- lifecycle ---------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

    async def _connect(self) -> None:
        """Connect Redis (lease authority) + the Kafka producer if not injected."""
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis.from_url(
                settings.REDIS_URL, decode_responses=True
            )
        if self._producer is None and self.runs_generation:
            self._producer = build_kafka_producer(
                settings.KAFKA_BOOTSTRAP_SERVERS, client_id=self.runner_id
            )
        self._leases = LeaseManager(self._redis, self.runner_id)

    async def _supervise_loop(self) -> None:
        """The generation supervision loop: heartbeat (5 s) + claimable scan (2 s)."""
        self._ready = True
        while not self._stop.is_set():
            now = time.monotonic()
            await self._heartbeat_once()
            if self.runs_generation and (now - self._last_scan) >= CLAIMABLE_SCAN_INTERVAL_S:
                self._last_scan = now
                with contextlib.suppress(Exception):
                    await self._scan_and_acquire()
            # Wake on stop or after the scan cadence (the shorter of the two clocks).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=CLAIMABLE_SCAN_INTERVAL_S
                )

    async def run(self) -> None:
        """Boot the health listener + connections, then run until signalled."""
        self._install_signal_handlers()
        await self._connect()

        app = web.Application()
        app.router.add_get("/healthz", self.healthz)
        app.router.add_get("/readyz", self.readyz)
        http = web.AppRunner(app)
        await http.setup()
        site = web.TCPSite(http, host="0.0.0.0", port=HEALTH_PORT)
        await site.start()
        # Expose df_ metrics on DF_METRICS_PORT (observability §4). The sink hosts
        # (buffer-writer/ws-pusher) run in threads inside this process, so this one
        # exposer covers the runner + buffer + ws metric families for this group.
        from django.conf import settings

        from observation.infra import metrics

        metrics.start_metrics_server(settings.DF_METRICS_PORT)
        if self.runs_sinks:
            self._start_sink_hosts()
        logger.info(
            "runner.boot",
            role=self.role,
            runner_id=self.runner_id,
            health_port=HEALTH_PORT,
        )
        try:
            await self._supervise_loop()
        finally:
            await self._shutdown(http)

    def _start_sink_hosts(self) -> None:
        """Start the §8.6 sink hosts in daemon threads (--role sinks).

        Two platform-shared sinks consume ``df.delivery.events.v1`` in separate
        consumer groups (§8.6):

        * **buffer-writer** (``df.sink.rest-buffer.v1``): strip_internal()s at ingest
          and COPYs into the hourly-partitioned event_buffer, committing offsets AFTER
          the insert (INV-DEL-3);
        * **ws-pusher** (``df.sink.websocket.v1``): strip_internal()s, stamps a
          per-stream ``frame_seq``, and ``group_send``s to the channel-layer group
          ``stream_{stream_id}`` for the live tail (§6.1), acking immediately
          (at-most-once).

        Each is a blocking Kafka consumer-group loop holding no Redis lease (§8.6) —
        the broker coordinates the group — so each lives in its own thread, not the
        asyncio lease loop. Failures are logged; health stays liveness-only.
        """
        import threading

        from runner.sinks.run import build_buffer_writer_host
        from runner.sinks.ws_run import build_ws_pusher_host

        builders = (
            ("buffer-writer-host", build_buffer_writer_host),
            ("ws-pusher-host", build_ws_pusher_host),
        )
        for name, build in builders:
            host = build(client_id=self.runner_id)
            self._sink_hosts.append(host)

            def _run(host: Any = host, name: str = name) -> None:
                try:
                    host.start()
                except Exception as exc:  # pragma: no cover - thread guard
                    logger.error("sink_host.crashed", host=name, error=str(exc))

            thread = threading.Thread(target=_run, name=name, daemon=True)
            thread.start()
            self._sink_threads.append(thread)
            logger.info("sink_host.started", host=name, role=self.role)

    async def _shutdown(self, http: web.AppRunner) -> None:
        """Cancel workers, release leases, close connections, stop the listener."""
        self._ready = False
        for host in self._sink_hosts:
            with contextlib.suppress(Exception):
                host.stop()
        for thread in self._sink_threads:
            with contextlib.suppress(Exception):
                thread.join(timeout=5.0)
        for shard in list(self._workers):
            await self._cancel_worker(shard, reason="shutdown")
        if self._leases is not None:
            with contextlib.suppress(Exception):
                await self._leases.shutdown()
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
        await http.cleanup()
        logger.info(
            "runner.shutdown", role=self.role, runner_id=self.runner_id
        )
