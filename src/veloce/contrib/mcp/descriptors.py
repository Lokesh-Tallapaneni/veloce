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

from dataclasses import dataclass


@dataclass(slots=True)
class MCPDescriptor:
    """Base for a served MCP primitive: its client-facing name and description."""

    name: str
    description: str
