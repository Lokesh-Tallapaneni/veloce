"""Application audit — what a deployment should know before it serves traffic.

Two questions share one mechanism. A misconfiguration ("this override names a
route that does not exist") must stop the boot; a posture finding ("the session
cookie is not `Secure`") must be reported without stopping anything. Severity is
what separates them, so a middleware writes one method and the caller decides
what a finding means: startup refuses to serve on an `error`, `veloce check`
exits non-zero on anything above `info`.

The audit also runs against an application that was only imported - `veloce
check` never starts one - so a check needing the finished route table says so
with `Middleware.audit_needs_routes` and is skipped until startup, rather than
reporting a route as missing when it is merely not registered yet.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from veloce._model_backend import resolve_return_model
from veloce.exceptions import VeloceError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce

#: How much a finding matters. `error` refuses the boot and fails `veloce
#: check`; `warning` fails `veloce check` only; `info` fails nothing and is
#: reported for awareness.
Severity = Literal["error", "warning", "info"]

# Ordering for "at least this severe" comparisons. A plain mapping rather than
# an enum: a finding is written as `Finding("...", "error")` with no import of
# an enum member, and the value survives JSON without conversion.
_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2}


class AuditFailed(VeloceError, ValueError):
    """An `error`-severity finding refused the application's startup.

    Also a `ValueError`, which is what a middleware raised for the same
    condition before findings carried a severity, so existing handling of a
    misconfigured middleware still catches it. The findings that caused the
    failure are on `.findings`.
    """

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        super().__init__("application audit failed: " + "; ".join(str(f) for f in findings))


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing an audit found.

    `message` states what is wrong; `fix` states what to do about it, kept
    separate so a tool can present or suppress the remedy independently. `id`
    is a stable handle for the finding, which is what `SILENCED_AUDIT_IDS`
    matches on, so an accepted finding can be turned off without turning the
    audit off.

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

    `routes_final` is False when the application was imported but never
    started, which is how `veloce check` runs it. A middleware that reads the
    route table sets `audit_needs_routes` and is not called at all in that
    case, so this flag is for the rarer check that can narrow its scope rather
    than skip.
    """

    app: Any
    routes_final: bool


def _response_contract_findings(app: Any) -> list[Finding]:
    """Findings about what each route says it returns.

    These used to be a second audit returning bare strings. Its own docstring
    distinguished two severities in prose - a `response_model` contradicting its
    return annotation was "a contradiction", an undocumented route was
    "informational rather than a failure" - and then flattened both into one
    untyped list. So the contradiction never reached `veloce check`'s exit code,
    could not be silenced, and was reported at startup only under `debug`.

    Saying it with severity instead means one audit, one vocabulary, one exit
    code and one silencing mechanism.
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


def _app_findings(app: Any) -> list[Finding]:
    """Findings about the application itself rather than about a middleware."""
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
    # `SECRET_KEY` is deliberately not checked here. Only the session middleware
    # reads it, and only it knows whether it was handed a key directly - reading
    # the config alone warned about a middleware constructed with an explicit
    # `secret_key=`, and stayed a warning for one that could not sign at all.
    return findings


def _unmatched_exclusions(app: Any) -> list[Finding]:
    """Report `exclude_middleware` names that match nothing registered.

    An exclusion is matched by `Middleware.middleware_name`, and an unmatched one
    is simply skipped at dispatch: the route keeps a middleware its author
    believes they opted out of, with nothing said at registration, startup, or
    dispatch. The documented use is `exclude_middleware=["CSRFMiddleware"]` on a
    webhook route, where a typo or a middleware registered under a custom `name=`
    leaves the route protected and 403ing.

    Only the *name* form can go unmatched. A class entry is matched by
    `isinstance`, so it cannot be misspelled - a wrong class is an import error
    where it is written - and excluding a class that happens not to be
    registered is a legitimate way to write a route that is safe either way.

    Registration cannot answer this - routes are commonly registered before
    middleware - so it is asked once the set is final.
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

    Veloce accepts middleware in three shapes and static handlers besides, and
    the audit used to walk only `_middlewares`. A `BaseHTTPMiddleware` that
    hardened every response was reported as absent; a `StaticFiles` pointed at a
    directory that does not exist warned at construction and reached
    `veloce check` not at all.

    The entries are heterogeneous by design - a plain function registered with
    `@app.middleware("http")` sits beside a class - so each is asked whether it
    can answer rather than assumed to. An ASGI middleware is held as a class
    with its keyword arguments, never instantiated until the stack is built, so
    only its class-level marker can be read.
    """
    yield from app._middlewares
    yield from app._http_middleware_funcs
    yield from app._static_handlers
    yield from app._auditables
    for entry in app._asgi_middleware:
        yield entry[0]


def run(app: Veloce, *, routes_final: bool = False) -> list[Finding]:
    """Collect every finding about `app`, newest concern last.

    Pass `routes_final=True` once the route table cannot change again - at
    startup, after the startup handlers and mounted sub-apps have registered
    theirs. Left False, a middleware declaring `audit_needs_routes` is skipped
    rather than asked a question it cannot answer yet.
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
