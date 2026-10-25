"""MCP descriptor base — the fields every served primitive shares.

`MCPTool`, `MCPResource`, and `MCPPrompt` subclass it, and each must be declared
``@dataclass(slots=True)`` or the primitive silently regains a `__dict__`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from veloce.contrib.mcp.icons import Icon


@dataclass(slots=True)
class MCPDescriptor:
    """Base for a served MCP primitive: its client-facing name and description."""

    name: str
    description: str
    # The optional display name the spec defines on every primitive. Keyword-only
    # so a subclass can still declare positional fields of its own.
    title: str | None = field(default=None, kw_only=True)
    # The optional MCP icon array; the empty default emits no ``icons`` key.
    icons: tuple[Icon, ...] = field(default=(), kw_only=True)
    # Maps an argument name to its opt-in ``completion/complete`` callable. Tools
    # carry none - the spec defines completion only for prompts and resources.
    completers: dict[str, Callable[..., Any]] = field(default_factory=dict, kw_only=True)
    # `_meta` the author attached to this primitive; the protocol reserves the
    # field for metadata it does not define. `None` emits nothing.
    meta: dict[str, Any] | None = field(default=None, kw_only=True)
    # The primitive's entry in whichever list method advertises it, memoized on
    # first listing: every field it derives from is fixed at registration.
    listing_entry: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    # The same entry as the modern revision defines it, built only when that
    # revision's shape differs from the handshake one.
    listing_entry_modern: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
