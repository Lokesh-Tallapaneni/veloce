"""What a mounted MCP endpoint reports to `veloce check` — its security posture.

An MCP endpoint executes tools. Mounted over a network transport with no `auth=`
it executes them for anyone who can reach the port, and with no `allowed_origins=`
it accepts a cross-origin `POST` from any page the operator's browser happens to
load, which is the DNS-rebinding case the MCP specification requires servers to
defend against.

The audit had no way to learn either. It walks the components registered on the
app, and `mount_mcp` registers routes - so an unauthenticated tool-execution
endpoint was invisible to a check that knows how to report a missing session
signing key.

`mount_mcp` records one of these per network transport instead. `stdio` records
none: it speaks over the process's own pipes, where there is no port to reach and
no `Origin` to validate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veloce.audit import Finding

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from veloce.audit import AuditContext


@dataclass(frozen=True, slots=True)
class MCPEndpointPosture:
    """One mounted MCP endpoint, as the audit sees it."""

    transport: str
    path: str
    authenticated: bool
    origin_checked: bool

    def audit(self, ctx: AuditContext) -> Iterator[Finding]:
        """Report what this endpoint exposes and what is not guarding it."""
        where = f"the MCP endpoint at {self.path} ({self.transport})"
        if not self.authenticated:
            yield Finding(
                f"{where} executes tools for any caller that can reach it.",
                "warning",
                fix=f"pass mount_mcp(transport={self.transport!r}, auth=MCPAuth(...))",
                id="mcp-endpoint-unauthenticated",
            )
        if not self.origin_checked:
            yield Finding(
                f"{where} does not validate the Origin header, so a page in the "
                "operator's browser can call it (DNS rebinding).",
                "warning",
                fix=(
                    f"pass mount_mcp(transport={self.transport!r}, "
                    "allowed_origins=['https://your.app'])"
                ),
                id="mcp-origin-unchecked",
            )


def record_endpoint(app: Any, transport: str, path: str, auth: Any, allowed_origins: Any) -> None:
    """Register one mounted endpoint's posture with the app's audit."""
    app._auditables.append(
        MCPEndpointPosture(
            transport=transport,
            path=path,
            authenticated=auth is not None,
            origin_checked=bool(allowed_origins),
        )
    )
