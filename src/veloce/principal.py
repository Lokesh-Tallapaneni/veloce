"""Principal — the authenticated identity for the current request, across doors.

A `Principal` is the identity established by whichever authentication ran for the
current request: a session/cookie/bearer check on the HTTP door, or the OAuth 2.1
token validation on the MCP door. Both populate the same request-scoped slot via
`set_principal`, and all downstream code (a `get_current_user` dependency, a
permission check, a tenant-scoped DB dependency) reads it through
`current_principal` - so authorization is written once and runs identically over
HTTP and MCP.

The identity is held in a `contextvars.ContextVar`, so it is isolated per request /
per task and never leaks across concurrent calls.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Principal:
    """The authenticated identity and granted scopes for the current request.

    Usage::

        from veloce import Principal, set_principal

        set_principal(Principal(subject="user-42", scopes={"mcp:tools"}))
    """

    # The authenticated subject - a user id, the token `sub` claim, a service
    # name. `None` for an anonymous or unauthenticated principal.
    subject: str | None = None
    # The granted authorization scopes (OAuth scopes / permissions), used for
    # per-tool and per-route authorization checks.
    scopes: frozenset[str] = frozenset()
    # The full set of token / identity claims, for application-specific use.
    claims: dict[str, Any] = field(default_factory=dict)
    # The raw credential the principal was established from, if the application
    # needs it (e.g. to call an upstream API as the user). `repr=False` keeps the
    # secret out of `repr()` / log lines that render the principal.
    token: str | None = field(default=None, repr=False)

    def has_scope(self, scope: str) -> bool:
        """Return whether the principal was granted `scope`."""
        return scope in self.scopes

    def has_scopes(self, scopes: Iterable[str]) -> bool:
        """Return whether the principal was granted every scope in `scopes`."""
        return set(scopes) <= self.scopes


# Request-scoped identity. A `ContextVar` (not request state) so the same accessor
# works on the HTTP path, the MCP transport, and inside a replayed MCP tool call,
# while staying isolated per request / per task.
_principal_var: ContextVar[Principal | None] = ContextVar("veloce_principal", default=None)


def current_principal() -> Principal | None:
    """Return the authenticated `Principal` for the current request, or `None`."""
    return _principal_var.get()


def set_principal(principal: Principal | None) -> None:
    """Set the authenticated `Principal` for the current request.

    Call this from whatever authenticates a request - an HTTP auth middleware or
    dependency, or the MCP transport's token verifier - so downstream code reads
    one identity through `current_principal`, regardless of which door the request
    arrived on.
    """
    _principal_var.set(principal)
