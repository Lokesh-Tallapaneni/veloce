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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

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
    # `SECRET_KEY` is deliberately not checked here. Only the session middleware
    # reads it, and only it knows whether it was handed a key directly - reading
    # the config alone warned about a middleware constructed with an explicit
    # `secret_key=`, and stayed a warning for one that could not sign at all.
    return findings


def run(app: Veloce, *, routes_final: bool = False) -> list[Finding]:
    """Collect every finding about `app`, newest concern last.

    Pass `routes_final=True` once the route table cannot change again - at
    startup, after the startup handlers and mounted sub-apps have registered
    theirs. Left False, a middleware declaring `audit_needs_routes` is skipped
    rather than asked a question it cannot answer yet.
    """
    ctx = AuditContext(app=app, routes_final=routes_final)
    findings = _app_findings(app)

    # One pass: whether anything hardens responses is a question about the set,
    # which no member can answer, so it is settled alongside collecting them.
    hardened = False
    for middleware in app._middlewares:
        cls = type(middleware)
        if cls.sets_hardening_headers:
            hardened = True
        if cls.audit_needs_routes and not routes_final:
            continue
        findings.extend(middleware.audit(ctx))

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
