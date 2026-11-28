"""Instrumentation hooks and the security-posture audits for `Veloce`."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from veloce.app._host import AppHost
from veloce.audit import run as audit_run

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce

    pass


class PostureMixin(AppHost):
    """Instrumentation registration and security / response-contract audits."""

    @property
    def instrumentation_hooks(self) -> tuple[Callable[..., Any], ...]:
        """The registered instrumentation hooks, in the order they will run.

        The read half of `add_instrumentation`. A tuple, so a caller cannot
        register or reorder through it.
        """
        return tuple(self._instrumentation)

    def add_instrumentation(
        self,
        hook: Callable[..., Any] | None = None,
        *,
        exclude_routes: Iterable[str] | None = None,
    ) -> Callable[..., Any]:
        """Register an observability instrumentation hook.

        `hook` is called once per finished HTTP request with a `RequestMetrics`
        record - method, concrete path, matched route *template*, status code
        and wall-clock duration in milliseconds. It may be sync or async, and
        one that raises is logged and skipped, so instrumentation never breaks
        a response.

        Returns `hook` unchanged, so it also works as a decorator, in either
        the no-argument or the keyword-argument form:

            @app.add_instrumentation
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

            @app.add_instrumentation(exclude_routes={"/health"})
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

        `exclude_routes` suppresses this hook for noisy routes - matched route
        *templates* such as `{"/health", "/metrics"}`. Matching is on the
        low-cardinality template resolved during routing, never the concrete
        attacker-controlled path, so there is no per-request regex and no
        path-normalisation bypass. The filter is applied in the core delivery
        loop, so every consumer honours it.

        With no hook registered the request path carries no instrumentation
        cost - not even a clock read.
        """
        self._assert_mutable()

        if hook is None:

            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                return self.add_instrumentation(fn, exclude_routes=exclude_routes)

            return decorator

        self._register_feature_state(self._instrumentation, hook)
        if exclude_routes is not None:
            excluded = frozenset(exclude_routes)
            if excluded:
                self._instrumentation_excludes[hook] = excluded
        return hook

    def register_auditable(self, component: Any) -> Any:
        """Register `component` to report to `veloce check` and `security_audit`.

        Middleware reports on itself through `Middleware.audit`, being already
        in the stack; this is for something that hardens or exposes the app
        *without* being middleware, such as a mounted MCP endpoint.

        `component` needs an `audit(ctx)` yielding `Finding`s, and may carry the
        `Auditable` class markers (`sets_hardening_headers`,
        `audit_needs_routes`). Returns `component`, so it also decorates.

        Usage::

            class TelemetryEndpoint:
                def audit(self, ctx):
                    if not self.authenticated:
                        yield Finding(
                            "telemetry endpoint accepts unauthenticated writes",
                            severity="error",
                            fix="pass auth=...",
                            id="telemetry-unauthenticated",
                        )

            app.register_auditable(TelemetryEndpoint())
        """
        self._assert_mutable()
        self._auditables.append(component)
        return component

    def security_audit(self) -> list[str]:
        """Return human-readable warnings about the current security posture.

        A rendering of `veloce.audit.run(self)`, whose structured form carries
        the severity, remedy and stable id a tool needs. An empty list means
        nothing was flagged.

        Hardening the app does not own - a reverse proxy terminating TLS - stays
        invisible, so a clean audit speaks about this app's middleware and not
        about the deployment around it.
        """
        return [str(finding) for finding in audit_run(cast("Veloce", self))]

    def response_contract_audit(self) -> list[str]:
        """Return human-readable findings about each route's response contract.

        A rendering of the `response-model-contradiction` and
        `routes-undocumented` findings from `veloce.audit.run(self)`. A
        `response_model=` naming a different model than the return annotation is
        a `warning`; no contract at all is `info`, since an HTML page, a redirect
        or a stream legitimately has none.
        """
        contract_ids = {"response-model-contradiction", "routes-undocumented"}
        return [
            str(finding)
            for finding in audit_run(cast("Veloce", self))
            if finding.id in contract_ids
        ]
