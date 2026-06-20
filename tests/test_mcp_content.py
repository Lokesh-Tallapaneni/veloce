"""MCP content-block family - polymorphic to_payload, annotations, slot discipline."""

from __future__ import annotations

import pytest

from veloce.contrib.mcp.content import (
    AudioContent,
    ContentBlock,
    ImageContent,
    TextContent,
)


def test_text_block_payload_matches_wire_shape():
    """A text block renders to the `{"type": "text", ...}` dict the client reads."""
    assert TextContent("hello").to_payload() == {"type": "text", "text": "hello"}


def test_image_block_payload_matches_wire_shape():
    """An image block renders to base64 data plus its media type."""
    assert ImageContent("Zm9v", "image/png").to_payload() == {
        "type": "image",
        "data": "Zm9v",
        "mimeType": "image/png",
    }


def test_audio_block_payload_matches_wire_shape():
    """An audio block renders to base64 data plus its media type."""
    assert AudioContent("Zm9v", "audio/wav").to_payload() == {
        "type": "audio",
        "data": "Zm9v",
        "mimeType": "audio/wav",
    }


def test_unset_annotations_are_omitted():
    """A block built without annotations serialises without an annotations key."""
    assert "annotations" not in TextContent("x").to_payload()


def test_annotations_merge_into_payload():
    """The base merges annotations into every block type's payload."""
    block = TextContent("x", annotations={"audience": ["user"], "priority": 0.5})
    payload = block.to_payload()
    assert payload["annotations"] == {"audience": ["user"], "priority": 0.5}
    assert payload["type"] == "text"


def test_subclasses_share_the_base():
    """Every concrete block is a `ContentBlock` for polymorphic rendering."""
    assert issubclass(TextContent, ContentBlock)
    assert issubclass(ImageContent, ContentBlock)
    assert issubclass(AudioContent, ContentBlock)


def test_blocks_stay_slotted():
    """Concrete blocks keep `__slots__` so they never regain a per-instance dict."""
    assert not hasattr(TextContent("x"), "__dict__")
    assert not hasattr(ImageContent("d", "image/png"), "__dict__")
    assert not hasattr(AudioContent("d", "audio/wav"), "__dict__")


def test_subclass_without_slots_is_rejected():
    """A subclass that forgets `__slots__` fails loudly at class creation."""
    with pytest.raises(TypeError, match="must declare __slots__"):

        class _Bad(ContentBlock):
            pass
