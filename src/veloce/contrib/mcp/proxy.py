"""Serving another MCP server's tools as if they were this app's own.

A gateway that fronts several MCP servers, or an app that wants one upstream
tool alongside its own, needs the upstream's catalogue to appear in its own
`tools/list` and its calls forwarded. `add_mcp_proxy` discovers an upstream's
tools once and registers each as a local tool that forwards when called.

**The connection stays with the application.** `add_mcp_proxy` takes a callable
that performs one JSON-RPC request against the upstream and returns its result.
Veloce does the discovery, the registration and the forwarding; the application
owns the client, and with it the retry policy, the credentials, the connection
pool and the timeouts — the parts that differ per deployment and that a framework
guessing at would get wrong.

Discovery is I/O, so it is awaited at setup rather than hidden behind a
decorator, and it runs before `mount_mcp` builds the registry::

    async def call_upstream(method: str, params: dict) -> dict:
        response = await client.post(UPSTREAM, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
        return response.json()["result"]

    await add_mcp_proxy(app, "upstream", call_upstream)
    app.mount_mcp(transport="http")

The upstream's own `inputSchema` is published unchanged: it is what the upstream
will validate against, so rebuilding it from a Python signature could only
disagree with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._handler_plan import build_plan
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.registry import MCPTool

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

# How many `tools/list` pages to walk before giving up. An upstream that keeps
# handing out a cursor would otherwise spin here forever.
_MAX_DISCOVERY_PAGES = 1000


async def add_mcp_proxy(
    app: Any,
    namespace: str,
    request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> list[str]:
    """Discover an upstream MCP server's tools and serve them from `app`.

    `namespace` prefixes every discovered tool name, so two upstreams offering a
    tool of the same name stay distinct and a local tool is never shadowed.
    Returns the local names registered, in the order the upstream listed them.

    Call this before `mount_mcp`, which builds the registry.
    """
    discovered = await _discover_tools(request)
    names: list[str] = []
    for entry in discovered:
        upstream_name = entry.get("name")
        if not isinstance(upstream_name, str) or not upstream_name:
            continue
        local_name = f"{namespace}_{upstream_name}" if namespace else upstream_name
        app._mcp_proxied_tools.append(_proxy_tool(local_name, upstream_name, entry, request))
        names.append(local_name)
    return names


async def _discover_tools(
    request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Read the upstream's whole tool catalogue, following its cursor."""
    entries: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    for _page in range(_MAX_DISCOVERY_PAGES):
        result = await request("tools/list", params)
        listed = result.get("tools") if isinstance(result, dict) else None
        if isinstance(listed, list):
            entries.extend(item for item in listed if isinstance(item, dict))
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if not cursor:
            return entries
        params = {"cursor": cursor}
    raise RuntimeError(
        "the upstream MCP server kept returning a pagination cursor; "
        f"stopped after {_MAX_DISCOVERY_PAGES} pages of tools/list"
    )


def _proxy_tool(
    local_name: str,
    upstream_name: str,
    entry: dict[str, Any],
    request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> MCPTool:
    """Build the local tool that forwards a call to `upstream_name`."""

    async def forward(ctx: MCPContext) -> Any:
        """Call the upstream tool and hand back what it answered.

        The arguments are read off the context rather than bound as parameters:
        the upstream's schema is published verbatim, so there is no Python
        signature here for the binder to map them onto.
        """
        return await request(
            "tools/call", {"name": upstream_name, "arguments": dict(ctx.arguments)}
        )

    forward.__name__ = local_name
    return MCPTool(
        name=local_name,
        description=entry.get("description") or f"Proxied tool {upstream_name}",
        title=entry.get("title"),
        handler=forward,
        plan=build_plan(forward),
        # Published verbatim: this is the contract the upstream will validate
        # against, so a schema rebuilt from the forwarder's signature could only
        # disagree with it.
        input_schema=entry.get("inputSchema") or {"type": "object", "properties": {}},
        output_schema=entry.get("outputSchema"),
        annotations=entry.get("annotations"),
        meta=entry.get("_meta"),
        passthrough_result=True,
    )
