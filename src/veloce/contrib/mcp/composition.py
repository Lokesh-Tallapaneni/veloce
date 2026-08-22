"""Composing one app's MCP surface out of the apps mounted inside it.

`app.mount(prefix, sub, expose_mcp=True)` publishes a sub-application's tools,
resources and prompts through the parent's MCP server. It is opt-in because
mounting an app for its HTTP routes should not silently hand an agent everything
that app can do.

Tools and prompts are renamed under the mount, so `/billing` turns `invoice` into
`billing_invoice` and two sub-apps may each register a tool of the same name.
Resources keep their URIs: a URI is the client-facing address of the thing rather
than a name this server may rewrite, so two sub-apps publishing the same URI is a
collision the registry reports rather than papers over.

A merged primitive is a copy, so the sub-app's own registry is untouched.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from veloce.contrib.mcp.descriptors import MCPDescriptor

T = TypeVar("T", bound="MCPDescriptor")


def mount_namespace(prefix: str) -> str:
    """Return the name prefix a mount contributes, so `/a/b` gives `a_b`."""
    return prefix.strip("/").replace("/", "_")


def renamed(item: T, namespace: str) -> T:
    """Copy `item` under the mount's namespace.

    The memoised listing entry is not carried over: it is declared `init=False`,
    so the copy starts without one and rebuilds it under the new name rather than
    advertising the name the primitive had inside the sub-app.
    """
    if not namespace:
        return item
    return replace(item, name=f"{namespace}_{item.name}")


def mcp_mounts(app: Any) -> Iterator[tuple[str, Any]]:
    """Yield `(namespace, sub_app)` for each sub-app mounted with `expose_mcp`."""
    for prefix, sub_app in getattr(app, "_mcp_mounts", ()):
        yield mount_namespace(prefix), sub_app
