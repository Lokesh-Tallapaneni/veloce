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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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

    `completers` maps one of the primitive's argument names to the opt-in callable
    that suggests completions for it (``completion/complete``). It is declared
    here so a prompt and a resource template share one argument-completer model
    rather than each carrying its own; it defaults to an empty mapping so a
    primitive without completers holds no extra state. Tools carry none - the MCP
    spec defines completion references only for prompts and resources.
    """

    name: str
    description: str
    title: str | None = field(default=None, kw_only=True)
    icons: tuple[Icon, ...] = field(default=(), kw_only=True)
    completers: dict[str, Callable] = field(default_factory=dict, kw_only=True)
    # `_meta` the author attached to this primitive's definition. The protocol
    # reserves the field for metadata it does not itself define, which is how an
    # extension carries its own data on a tool, resource or prompt. It lives here
    # so all three share one declaration; `None` emits nothing.
    meta: dict[str, Any] | None = field(default=None, kw_only=True)
    # The primitive's entry in whichever list method advertises it, built on the
    # first listing and reused after. Every field an entry is derived from is
    # fixed once the primitive is registered, so one entry serves every request
    # from every client of a given protocol revision. Where the revisions define
    # different shapes - a tool's `execution`, which the modern revision removed
    # - the second shape is memoized separately in `listing_entry_modern` rather
    # than rebuilt per listing. It lives on the base for the same reason `title` and `icons` do -
    # declared once rather than copied per subclass. A resource is listed either
    # as a concrete URI or as a template, never both (`is_template` partitions
    # them), so a single field covers both shapes. Excluded from `__init__` /
    # `repr` / `eq` so it stays an internal memo rather than part of the
    # primitive's identity.
    listing_entry: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    # The same entry as the modern revision defines it, built only when that
    # revision's shape differs from the handshake one. `None` means "not built
    # yet"; a primitive whose shape is revision-independent never allocates it.
    listing_entry_modern: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
