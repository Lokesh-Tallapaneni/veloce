"""Trusted-host middleware — validates `Host` against an allow-list (RFC 9110 Sec. 7.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import veloce.status as status
from veloce._constants import (
    HEADER_HOST,
)
from veloce._internal import _extract_host
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    pass


# ── Host validation ───────────────────────────────────────
class TrustedHostMiddleware(Middleware):
    """Validates Host header against an allow-list.

    Supports literal hostnames, the catch-all `*`, and subdomain wildcards
    of the form `*.example.com` (matches `api.example.com`,
    `a.b.example.com`, etc. - never the bare `example.com`). Matching is
    case-insensitive; the port portion of `Host:` is stripped before
    comparison (RFC 9110 Sec. 7.2).

    A host that fails the check is refused with `400 Bad Request`, because the
    request named a host this server does not answer for - the request itself is
    malformed, not the credential it carried. Pass `status_code=403` to report it
    as a refusal instead, or any other 4xx an edge proxy or log pipeline keys on.
    """

    def __init__(
        self,
        allowed_hosts: list[str],
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        name: str | None = None,
    ) -> None:
        """Set the allow-list.

        Args:
            allowed_hosts: Literal hosts, `*`, or `*.example.com` subdomain patterns.
            status_code: The 4xx status a refused host receives.
            name: Name for `exclude_middleware`.

        Raises:
            ValueError: If `status_code` is not a 4xx.

        """
        super().__init__(name=name)
        # `ValueError`, not `assert`: `python -O` strips asserts, and a non-4xx
        # status makes the refusal read as a success, a redirect or a fault.
        if not 400 <= status_code < 500:
            raise ValueError("TrustedHostMiddleware status_code must be a 4xx client error")
        self._status_code = status_code
        # Split at construction so the hot path stays O(1) for the common case.
        self._exact: frozenset[str] = frozenset(
            h.lower() for h in allowed_hosts if not h.startswith("*.")
        )
        # Leading dot baked in so the per-request check is one `endswith`.
        self._wildcard_suffixes_dotted: tuple[str, ...] = tuple(
            "." + h.lower()[2:] for h in allowed_hosts if h.startswith("*.")
        )
        self._allow_all = "*" in self._exact

    def is_host_allowed(self, host: str) -> bool:
        """Whether `host` (bare hostname, no port) passes the allow-list.

        Public so the WebSocket dispatch path can apply the same check -
        a WebSocket handshake never reaches an HTTP middleware's
        `process_request`.
        """
        if self._allow_all or host in self._exact:
            return True
        return any(host.endswith(suffix) for suffix in self._wildcard_suffixes_dotted)

    async def process_request(self, request: Request) -> Response | None:
        """Reject requests whose Host header is not in the allow-list."""
        # A replayed MCP call carries no Host, and the transport POST that carried
        # it already passed this check.
        if request.is_mcp:
            return None
        # Shared with the request-side parser so the IPv6 and port shapes agree.
        host = _extract_host(request.headers.get(HEADER_HOST, ""))
        if not self.is_host_allowed(host):
            return Response(status_code=self._status_code, body=b"Invalid host header")
        return None
