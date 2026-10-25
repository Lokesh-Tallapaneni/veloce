"""Principal — the authenticated identity for the current request, across doors.

An HTTP session/bearer check and MCP OAuth 2.1 token validation populate the same
request-scoped slot, so authorization is written once and runs over both doors.
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
    # The raw credential the principal was established from. `repr=False` keeps
    # the secret out of `repr()` / log lines that render the principal.
    token: str | None = field(default=None, repr=False)

    def has_scope(self, scope: str) -> bool:
        """Return whether the principal was granted `scope`."""
        return scope in self.scopes

    def has_scopes(self, scopes: Iterable[str]) -> bool:
        """Return whether the principal was granted every scope in `scopes`."""
        return set(scopes) <= self.scopes


# A `ContextVar` (not request state) so one accessor serves the HTTP path, the MCP
# transport and a replayed tool call, isolated per request / per task.
_principal_var: ContextVar[Principal | None] = ContextVar("veloce_principal", default=None)


def current_principal() -> Principal | None:
    """Return the authenticated `Principal` for the current request, or `None`."""
    return _principal_var.get()


def set_principal(principal: Principal | None) -> None:
    """Set the authenticated `Principal` for the current request.

    Call it from whatever authenticates the request, on either door, so downstream
    code reads one identity through `current_principal`.
    """
    _principal_var.set(principal)
