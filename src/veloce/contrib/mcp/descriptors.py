"""MCP descriptor base — the fields every served primitive shares.

A tool, a resource, and a prompt are all named, described primitives the server
advertises to the client. `MCPDescriptor` holds the metadata common to all of
them so a field that applies to every primitive is declared once; `MCPTool`,
`MCPResource`, and `MCPPrompt` subclass it and add only their distinct fields.

The base is a slotted dataclass: `slots=True` is mandatory on every subclass so
a primitive never silently regains a per-instance `__dict__`. A subclass that is
not a slotted dataclass would gain a `__dict__`, so the discipline is enforced
by always decorating concretes with ``@dataclass(slots=True)`` rather than by an
``__init_subclass__`` guard — the guard cannot run on the pre-slots class that
``@dataclass(slots=True)`` rebuilds in its second pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veloce.contrib.mcp.icons import Icon


@dataclass(slots=True)
class MCPDescriptor:
    """Base for a served MCP primitive: its client-facing name and description.

    `title` is the optional human-facing display name the spec defines on every
    primitive (a tool, a resource, a prompt). It lives here so the field is
    declared once and each subclass inherits it instead of carrying a private
    copy; a subclass without a route summary leaves it `None`. It is keyword-only
    so a subclass can still declare its own positional fields without a default
    (a default-valued base field would otherwise force every later field to have
    one).

    `icons` is the optional MCP icon array every primitive may carry. It lives
    here for the same reason as `title` - declared once, inherited by every
    subclass - and defaults to the empty tuple so a primitive without icons holds
    no extra state and emits no ``icons`` key.
    """

    name: str
    description: str
    title: str | None = field(default=None, kw_only=True)
    icons: tuple[Icon, ...] = field(default=(), kw_only=True)
