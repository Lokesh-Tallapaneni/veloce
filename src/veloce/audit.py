"""Application audit — what a deployment should know before it serves traffic.

Severity separates a misconfiguration from a posture finding: startup refuses to
serve on an `error`, `veloce check` exits non-zero on anything above `info`.

The audit also runs against an application that was only imported, so a check
needing the finished route table says so with `Middleware.audit_needs_routes`
and is skipped until startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from veloce._model_backend import resolve_return_model
from veloce.exceptions import VeloceError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce

#: How much a finding matters. `error` refuses the boot and fails `veloce check`;
#: `warning` fails `veloce check` only; `info` fails nothing.
Severity = Literal["error", "warning", "info"]

# Ordering for "at least this severe" comparisons. A plain mapping rather than an
# enum so a severity is written as a literal and survives JSON unconverted.
_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2}


class AuditFailed(VeloceError, ValueError):
    """An `error`-severity finding refused the application's startup.

    Also a `ValueError`, so existing handling of a misconfigured middleware still
    catches it. The findings that caused the failure are on `.findings`.
    """

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        super().__init__("application audit failed: " + "; ".join(str(f) for f in findings))


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing an audit found.

    `fix` is kept separate from `message` so a tool can present or suppress the
    remedy independently; `id` is what `SILENCED_AUDIT_IDS` matches on.

    Usage::

        Finding(
            "TENANT_SIGNING_KEY is not set - tenant headers are unverified.",
            severity="error",
            fix="set TENANT_SIGNING_KEY",
            id="tenant-signing-key-missing",
        )
    """

    message: str
    severity: Severity = "warning"
    fix: str | None = None
    id: str | None = None

    def at_least(self, severity: Severity) -> bool:
        """Whether this finding is at or above `severity`."""
        return _RANK[self.severity] >= _RANK[severity]

    def __str__(self) -> str:
        return f"{self.message} ({self.fix})" if self.fix else self.message


@dataclass(frozen=True, slots=True)
class AuditContext:
    """What an audited middleware is given.

    `routes_final` is False when the application was imported but never started.
    It is for the rarer check that narrows its scope rather than skipping.
    """

    app: Veloce
    routes_final: bool


def _response_contract_findings(app: Veloce) -> list[Finding]:
    """Report findings about what each route says it returns.

    A `response_model` contradicting its return annotation is a `warning`; a
    route publishing no schema at all is `info`.
    """
    findings: list[Finding] = []
    undocumented: list[str] = []
    for method, path, info in app.iter_routes(include_hidden=True):
        declared = info.response_model
        annotated = resolve_return_model(info.handler)
        if declared is None:
            if annotated is None:
                undocumented.append(f"{method} {path}")
            continue
        if annotated is not None and annotated is not declared:
            findings.append(
                Finding(
                    f"{method} {path} declares response_model="
                    f"{getattr(declared, '__name__', declared)!s} but its return annotation "
                    f"names {getattr(annotated, '__name__', annotated)!s}; the documented "
                    "response and the annotation disagree.",
                    severity="warning",
                    fix="make the annotation and response_model name the same model",
                    id="response-model-contradiction",
                )
            )
    if undocumented:
        listed = ", ".join(sorted(undocumented)[:10])
        more = f", and {len(undocumented) - 10} more" if len(undocumented) > 10 else ""
        findings.append(
            Finding(
                f"{len(undocumented)} route(s) publish no response schema: {listed}{more}.",
                severity="info",
                fix="annotate the handler's return type, or pass response_model=",
                id="routes-undocumented",
            )
        )
    return findings


def _app_findings(app: Veloce) -> list[Finding]:
    """Report findings about the application rather than about a middleware."""
    findings: list[Finding] = []
    if app.debug:
        findings.append(
            Finding(
                "DEBUG is enabled - tracebacks leak source and internals.",
                severity="warning",
                fix="disable it before deploying to production",
                id="debug-enabled",
            )
        )
    findings.extend(_response_contract_findings(app))
    # `SECRET_KEY` is deliberately not checked here: only the session middleware
    # knows whether it was handed a key directly rather than through the config.
    return findings


def _unmatched_exclusions(app: Veloce) -> list[Finding]:
    """Report `exclude_middleware` names that match nothing registered.

    An unmatched name is skipped at dispatch, so the route silently keeps a
    middleware its author believes they opted out of. Only the *name* form can go
    unmatched: a class entry is matched by `isinstance`. Routes are commonly
    registered before middleware, so it is asked once the set is final.
    """
    registered = {mw.middleware_name for mw in app._middlewares}
    unmatched: dict[str, str] = {}
    for method, path, info in app.iter_routes(include_hidden=True):
        excluded = info.excluded_middleware
        if excluded is None:
            continue
        for name in excluded[0]:
            if name not in registered:
                unmatched.setdefault(name, f"{method} {path}")
    if not unmatched:
        return []
    known = ", ".join(sorted(registered)) or "none registered"
    return [
        Finding(
            f"Route {where} excludes {name!r}, which no registered middleware is "
            f"named - the exclusion does nothing. Registered names: {known}.",
            severity="warning",
            fix="Match the name a middleware reports, or drop the exclusion.",
            id="exclude-middleware-unmatched",
        )
        for name, where in sorted(unmatched.items())
    ]


def _registered(app: Veloce) -> Iterator[Any]:
    """Every registered component that could report on itself.

    Veloce accepts middleware in three shapes and static handlers besides, so
    walking `_middlewares` alone would miss most of them. The entries are
    heterogeneous, so each is asked whether it can answer rather than assumed to;
    an ASGI middleware is still a class, so only its class marker can be read.
    """
    yield from app._middlewares
    yield from app._http_middleware_funcs
    yield from app._static_handlers
    yield from app._auditables
    for entry in app._asgi_middleware:
        yield entry[0]


def run(app: Veloce, *, routes_final: bool = False) -> list[Finding]:
    """Collect every finding about `app`, newest concern last.

    Pass `routes_final=True` once the route table cannot change again. Left
    False, a middleware declaring `audit_needs_routes` is skipped.
    """
    ctx = AuditContext(app=app, routes_final=routes_final)
    findings = _app_findings(app)
    # Read from `ctx`, not the parameter: the field is what a third-party
    # `audit(ctx)` sees, so the two must not be able to disagree.
    if ctx.routes_final:
        findings.extend(_unmatched_exclusions(app))

    # One pass: whether anything hardens responses is a question about the set,
    # which no member can answer, so it is settled alongside collecting them.
    hardened = False
    for component in _registered(app):
        cls = component if isinstance(component, type) else type(component)
        if getattr(cls, "sets_hardening_headers", False):
            hardened = True
        if getattr(cls, "audit_needs_routes", False) and not ctx.routes_final:
            continue
        report = getattr(component, "audit", None)
        if report is not None:
            findings.extend(report(ctx))

    if not hardened:
        findings.append(
            Finding(
                "No middleware sets hardening headers - responses ship without nosniff, "
                "frame-deny or a referrer policy.",
                severity="warning",
                fix="app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))",
                id="hardening-headers-missing",
            )
        )

    silenced = app.config.get("SILENCED_AUDIT_IDS")
    if not silenced:
        return findings
    quiet = frozenset(silenced)
    return [f for f in findings if f.id not in quiet]
