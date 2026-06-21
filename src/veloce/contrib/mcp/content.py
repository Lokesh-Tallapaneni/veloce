"""MCP content blocks — the polymorphic family behind every result content entry.

A tool result, a prompt message, and a typed binary return all carry MCP
*content blocks*: tagged objects (`{"type": "text" | "image" | "audio", ...}`)
the client renders. `ContentBlock` is the base every block shares; `TextContent`,
`ImageContent`, and `AudioContent` are the concrete kinds the server emits today.
Each subclass renders itself through `to_payload()`, so a result builder shapes a
block by calling the base method and the subclass decides the wire form — a new
block kind is added by subclassing, not by hand-writing another inline dict.

The base owns the optional `annotations` field (audience / priority /
lastModified per the MCP spec) and merges it into every block's payload, so the
metadata is added in one place rather than re-threaded through each call site.
`annotations` defaults to `None` and is omitted from the payload when unset, so a
block built without annotations serialises to exactly the dict the call sites
built by hand before this family existed.

Each block is slotted with an `__init_subclass__` guard so a subclass that
forgets `__slots__` fails loudly instead of silently regaining a `__dict__`.
"""

from __future__ import annotations

from typing import Any


class ContentBlock:
    """Base MCP content block: its `type` payload plus optional annotations."""

    __slots__ = ("annotations",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A subclass without its own __slots__ silently regains a per-instance
        # __dict__, defeating the slotted base; fail at class creation instead.
        super().__init_subclass__(**kwargs)
        if "__slots__" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare __slots__")

    def __init__(self, *, annotations: dict[str, Any] | None = None) -> None:
        self.annotations = annotations

    def _body(self) -> dict[str, Any]:
        """Return this block's `type`-tagged fields, without shared annotations."""
        raise NotImplementedError

    def to_payload(self) -> dict[str, Any]:
        """Render this block as its MCP content-block dict, merging annotations."""
        payload = self._body()
        if self.annotations is not None:
            payload["annotations"] = self.annotations
        return payload


class TextContent(ContentBlock):
    """A text content block carrying a plain-text value."""

    __slots__ = ("text",)

    def __init__(self, text: str, *, annotations: dict[str, Any] | None = None) -> None:
        super().__init__(annotations=annotations)
        self.text = text

    def _body(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


class ImageContent(ContentBlock):
    """An image content block carrying base64 bytes and their media type."""

    __slots__ = ("data", "mime_type")

    def __init__(
        self, data: str, mime_type: str, *, annotations: dict[str, Any] | None = None
    ) -> None:
        super().__init__(annotations=annotations)
        self.data = data
        self.mime_type = mime_type

    def _body(self) -> dict[str, Any]:
        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


class AudioContent(ContentBlock):
    """An audio content block carrying base64 bytes and their media type."""

    __slots__ = ("data", "mime_type")

    def __init__(
        self, data: str, mime_type: str, *, annotations: dict[str, Any] | None = None
    ) -> None:
        super().__init__(annotations=annotations)
        self.data = data
        self.mime_type = mime_type

    def _body(self) -> dict[str, Any]:
        return {"type": "audio", "data": self.data, "mimeType": self.mime_type}
