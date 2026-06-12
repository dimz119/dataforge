"""Phase-1 runner supervisor: heartbeat loop + internal health listener on :8081
(backend-architecture §8.1; deployment-architecture §2.1).

Phase 5 replaces this with the full asyncio supervisor running shard workers
(`generation`) and sink consumers (`sinks`) as supervised tasks.
"""

import asyncio
import contextlib
import signal
import time

import structlog
from aiohttp import web
from django.conf import settings

logger = structlog.get_logger("dataforge.runner")

HEALTH_PORT = 8081


class Supervisor:
    """Heartbeats at HEARTBEAT_MS and serves /healthz + /readyz on :8081."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.heartbeat_interval_s = settings.HEARTBEAT_MS / 1000.0
        self._last_beat = time.monotonic()
        self._beats = 0
        self._stop = asyncio.Event()

    # -- health listener -------------------------------------------------------

    async def healthz(self, request: web.Request) -> web.Response:
        """Liveness: heartbeat updated within 2x its interval (observability §6.1)."""
        fresh = (time.monotonic() - self._last_beat) <= 2 * self.heartbeat_interval_s
        status = 200 if fresh else 503
        return web.json_response(
            {"status": "ok" if fresh else "wedged", "service": settings.DF_SERVICE},
            status=status,
        )

    async def readyz(self, request: web.Request) -> web.Response:
        """Phase-1 stub readiness: process-up only.

        Phase 5 replaces this with lease/reconcile logic and the runner gating
        set (postgres, redis, kafka — observability §6.2).
        """
        return web.json_response(
            {
                "status": "ready",
                "components": {},
                "gating": [],
                "release": settings.RELEASE,
            }
        )

    # -- heartbeat -------------------------------------------------------------

    async def heartbeat(self) -> None:
        while not self._stop.is_set():
            self._last_beat = time.monotonic()
            self._beats += 1
            logger.info(
                "runner.heartbeat",
                message=f"runner heartbeat #{self._beats} (role={self.role})",
                ctx={"role": self.role, "beats": self._beats},
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.heartbeat_interval_s
                )

    # -- lifecycle ---------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

    async def run(self) -> None:
        self._install_signal_handlers()

        app = web.Application()
        app.router.add_get("/healthz", self.healthz)
        app.router.add_get("/readyz", self.readyz)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=HEALTH_PORT)
        await site.start()

        logger.info(
            "runner.boot",
            message=f"runner started (role={self.role}, health on :{HEALTH_PORT})",
            ctx={"role": self.role, "health_port": HEALTH_PORT},
        )
        try:
            await self.heartbeat()
        finally:
            await runner.cleanup()
            logger.info(
                "runner.shutdown",
                message=f"runner stopped (role={self.role})",
                ctx={"role": self.role, "beats": self._beats},
            )
