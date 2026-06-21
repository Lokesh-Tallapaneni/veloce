"""MCP icon descriptors — the optional visual a client renders for a primitive.

The Model Context Protocol lets a server attach an ``icons`` array to a tool, a
prompt, a resource, and its own implementation info so a client can show a glyph
next to the primitive. Each entry is an `Icon`: a ``src`` URI (the required
field) plus an optional media type and an optional list of rendered sizes.

`Icon` is an opt-in, immutable value a registration site builds explicitly; a
primitive with no icons carries an empty tuple and emits no ``icons`` key, so the
wire form is unchanged for a server that does not use them. `render_icons`
collapses a tuple of `Icon` to the spec's list-of-dicts (or ``None`` when empty),
so every descriptor emits its icons through one helper rather than re-shaping the
array at each call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Icon:
    """One MCP icon: a source URI plus optional media type and rendered sizes."""

    src: str
    mime_type: str | None = None
    # Rendered sizes the icon is available in (e.g. ``("48x48", "96x96")`` or
    # ``("any",)`` for a scalable source), mirroring the HTML ``sizes`` token
    # list. Omitted from the payload when empty.
    sizes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Render this icon as its MCP icon dict, omitting unset optionals."""
        payload: dict[str, Any] = {"src": self.src}
        if self.mime_type is not None:
            payload["mimeType"] = self.mime_type
        if self.sizes:
            payload["sizes"] = list(self.sizes)
        return payload


def coerce_icons(icons: Sequence[Icon] | None) -> tuple[Icon, ...]:
    """Normalise a user-supplied icons sequence into a stored tuple.

    `None` (the common, icon-free case) and an empty sequence both yield the
    empty tuple, so a primitive without icons stays zero-extra-state.
    """
    if not icons:
        return ()
    return tuple(icons)


def render_icons(icons: tuple[Icon, ...]) -> list[dict[str, Any]] | None:
    """Render a primitive's icons for its list/info payload, or `None` if empty."""
    if not icons:
        return None
    return [icon.to_payload() for icon in icons]
