"""MCP icon descriptors — the optional visual a client renders for a primitive.

A primitive with no icons carries an empty tuple and emits no ``icons`` key, so
the wire form is unchanged for a server that does not use them.
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
    # Mirrors the HTML ``sizes`` token list: ``("48x48",)``, or ``("any",)``
    # for a scalable source.
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
    """Normalise a user-supplied icons sequence into a stored tuple."""
    if not icons:
        return ()
    return tuple(icons)


def render_icons(icons: tuple[Icon, ...]) -> list[dict[str, Any]] | None:
    """Render a primitive's icons for its list/info payload, or `None` if empty."""
    if not icons:
        return None
    return [icon.to_payload() for icon in icons]
