"""Health probes — liveness and readiness endpoints for an orchestrator.

Kubernetes (and ECS, Nomad, and most load balancers) ask two different
questions, and answering them with one endpoint is the usual cause of a bad
rollout:

- **Liveness** — "is this process wedged?" A failure gets the container
  *killed and restarted*. It must not depend on a database, or a brief
  dependency outage restarts every replica at once and turns a degradation
  into an outage.
- **Readiness** — "should this replica receive traffic?" A failure only
  removes the pod from the load-balancer pool. This is where dependency
  checks belong, and it is what must flip to failing *before* shutdown
  drains connections, so the orchestrator stops sending new work while
  in-flight requests finish.

Installed as a plugin, so an application that does not want probe endpoints
registers no routes and pays nothing::

    from veloce import Veloce
    from veloce.health import HealthPlugin

    app = Veloce()
    health = app.install(HealthPlugin())

    @health.readiness_check("database")
    async def db_ready() -> bool:
        return await pool.fetchval("SELECT 1") == 1
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

import veloce.status as status
from veloce.http.response import JSONResponse

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce

# A readiness check returns True (ready) or False, sync or async.
ReadinessCheck = Callable[[], "bool | Awaitable[bool]"]


class HealthPlugin:
    """Serve liveness and readiness probes, and gate readiness on shutdown.

    Usage::

        from veloce import Veloce
        from veloce.health import HealthPlugin

        app = Veloce()
        health = app.install(HealthPlugin())

        @health.readiness_check("cache")
        async def cache_ready() -> bool:
            return await redis.ping()

    `/livez` reports whether the process and its event loop are running; it
    deliberately ignores dependency checks, because restarting a container
    cannot fix someone else's database.

    `/readyz` reports whether this replica should receive traffic: startup has
    completed, shutdown has not begun, and every registered check passes. A
    failing check yields `503` with a per-check body naming what failed, so a
    probe failure is diagnosable from the response alone rather than only from
    logs.

    Checks run concurrently and share one `timeout`; a check that hangs is
    reported as failed rather than holding the probe open until the
    orchestrator's own timeout fires.
    """

    name = "health"

    def __init__(
        self,
        *,
        liveness_path: Annotated[str, Doc("Route serving the liveness probe.")] = "/livez",
        readiness_path: Annotated[str, Doc("Route serving the readiness probe.")] = "/readyz",
        timeout: Annotated[
            float,
            Doc("Seconds all readiness checks share before being reported as failed."),
        ] = 2.0,
        include_in_schema: Annotated[
            bool,
            Doc("Publish the probe routes in the OpenAPI schema."),
        ] = False,
    ) -> None:
        self.liveness_path = liveness_path
        self.readiness_path = readiness_path
        self.timeout = timeout
        self.include_in_schema = include_in_schema
        self._checks: dict[str, ReadinessCheck] = {}
        self._started = False
        self._draining = False

    # ── Registration ──────────────────────────────────────

    def readiness_check(
        self, name: Annotated[str, Doc("Name reported for this check in the probe body.")]
    ) -> Callable[[ReadinessCheck], ReadinessCheck]:
        """Register a readiness check under `name`.

        The check returns True when this replica can serve traffic. Raising is
        treated as not-ready, so a check does not need its own try/except.
        """

        def decorator(func: ReadinessCheck) -> ReadinessCheck:
            self._checks[name] = func
            return func

        return decorator

    def start_draining(self) -> None:
        """Mark the replica as draining so `/readyz` starts failing.

        Call this when a shutdown signal arrives, *before* connections are
        drained: the orchestrator then stops routing new requests here while
        in-flight ones finish. Veloce's own shutdown calls it automatically.
        """
        self._draining = True

    @property
    def draining(self) -> bool:
        """Whether the replica has begun shutting down."""
        return self._draining

    # ── Plugin protocol ───────────────────────────────────

    def install(self, app: Veloce) -> None:
        """Register the probe routes and the lifecycle hooks that gate them."""

        @app.on_startup
        async def _mark_started() -> None:
            self._started = True

        @app.on_shutdown
        async def _mark_draining() -> None:
            self.start_draining()

        @app.get(self.liveness_path, include_in_schema=self.include_in_schema)
        async def _livez() -> Any:
            # Reaching a handler proves the loop is scheduling work, which is
            # the whole question liveness asks. Dependency state is deliberately
            # not consulted: a restart cannot fix an upstream outage.
            return JSONResponse({"status": "alive"})

        @app.get(self.readiness_path, include_in_schema=self.include_in_schema)
        async def _readyz() -> Any:
            return await self._readiness_response()

    # ── Probe body ────────────────────────────────────────

    async def _readiness_response(self) -> JSONResponse:
        if not self._started:
            return JSONResponse(
                {"status": "not_ready", "reason": "starting"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if self._draining:
            return JSONResponse(
                {"status": "not_ready", "reason": "draining"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not self._checks:
            return JSONResponse({"status": "ready", "checks": {}})

        names = list(self._checks)
        results = await self._run_checks(names)
        failed = [n for n, ok in zip(names, results, strict=True) if not ok]
        body = {
            "status": "not_ready" if failed else "ready",
            "checks": dict(zip(names, ["pass" if ok else "fail" for ok in results], strict=True)),
        }
        code = status.HTTP_503_SERVICE_UNAVAILABLE if failed else status.HTTP_200_OK
        return JSONResponse(body, status_code=code)

    async def _run_checks(self, names: list[str]) -> list[bool]:
        """Run every check concurrently under one shared timeout."""

        async def run(name: str) -> bool:
            check = self._checks[name]
            try:
                result = check()
                if inspect.isawaitable(result):
                    result = await result
                return bool(result)
            except Exception:
                # A raising check is not-ready. Swallowed deliberately: a probe
                # that 500s tells the orchestrator nothing it can act on, while
                # a 503 with the failing check named does.
                return False

        try:
            return await asyncio.wait_for(
                asyncio.gather(*(run(name) for name in names)),
                timeout=self.timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            # The gather is cancelled; report every check as failed rather than
            # holding the probe open until the orchestrator gives up on us.
            return [False] * len(names)
