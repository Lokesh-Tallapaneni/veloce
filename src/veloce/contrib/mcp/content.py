"""MCP content blocks — the polymorphic family behind every result content entry.

A new block kind is added by subclassing and implementing `_body()`. The base
owns the spec's optional `annotations` field and merges it into every payload.
"""

from __future__ import annotations

from typing import Any

from veloce._internal import _require_slots


class ContentBlock:
    """Base MCP content block: its `type` payload plus optional annotations."""

    __slots__ = ("annotations",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A subclass without its own __slots__ silently regains a per-instance
        # __dict__, defeating the slotted base; fail at class creation instead.
        super().__init_subclass__(**kwargs)
        _require_slots(cls)

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


class ResourceLink(ContentBlock):
    """A `resource_link` block referencing a resource by URI rather than inlining it.

    The client follows the `uri` with a `resources/read`. `name` is required by
    the spec; the remaining hints are omitted when unset.
    """

    __slots__ = ("uri", "name", "title", "description", "mime_type")

    def __init__(
        self,
        uri: str,
        name: str,
        *,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(annotations=annotations)
        self.uri = uri
        self.name = name
        self.title = title
        self.description = description
        self.mime_type = mime_type

    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "resource_link", "uri": self.uri, "name": self.name}
        if self.title is not None:
            body["title"] = self.title
        if self.description is not None:
            body["description"] = self.description
        if self.mime_type is not None:
            body["mimeType"] = self.mime_type
        return body


class EmbeddedResource(ContentBlock):
    """A `resource` block inlining a resource's contents directly in the result.

    Carries the resource-contents entry (`uri`/`mimeType` plus `text` or base64
    `blob`) so an agent reads the data without a follow-up `resources/read`.
    """

    __slots__ = ("resource",)

    def __init__(
        self, resource: dict[str, Any], *, annotations: dict[str, Any] | None = None
    ) -> None:
        super().__init__(annotations=annotations)
        self.resource = resource

    def _body(self) -> dict[str, Any]:
        return {"type": "resource", "resource": self.resource}
