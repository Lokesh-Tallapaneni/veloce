"""MCP content-block family - polymorphic to_payload, annotations, slot discipline."""

from __future__ import annotations

import pytest

from veloce.contrib.mcp.content import (
    AudioContent,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
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


def test_resource_link_payload_matches_wire_shape():
    """A resource_link block carries the uri/name plus optional hint fields."""
    block = ResourceLink(
        "res://config", "config", title="App config", description="d", mime_type="text/plain"
    )
    assert block.to_payload() == {
        "type": "resource_link",
        "uri": "res://config",
        "name": "config",
        "title": "App config",
        "description": "d",
        "mimeType": "text/plain",
    }


def test_resource_link_omits_unset_optionals():
    """A resource_link with only uri/name emits no title/description/mimeType."""
    assert ResourceLink("res://a", "a").to_payload() == {
        "type": "resource_link",
        "uri": "res://a",
        "name": "a",
    }


def test_embedded_resource_payload_matches_wire_shape():
    """An embedded resource inlines its resource-contents entry under `resource`."""
    contents = {"uri": "res://a", "mimeType": "text/plain", "text": "hi"}
    assert EmbeddedResource(contents).to_payload() == {
        "type": "resource",
        "resource": contents,
    }


def test_resource_blocks_carry_annotations():
    """Resource-link and embedded blocks inherit the base annotations merge."""
    link = ResourceLink("res://a", "a", annotations={"priority": 0.2})
    embedded = EmbeddedResource({"uri": "res://a"}, annotations={"audience": ["user"]})
    assert link.to_payload()["annotations"] == {"priority": 0.2}
    assert embedded.to_payload()["annotations"] == {"audience": ["user"]}


def test_subclasses_share_the_base():
    """Every concrete block is a `ContentBlock` for polymorphic rendering."""
    assert issubclass(TextContent, ContentBlock)
    assert issubclass(ImageContent, ContentBlock)
    assert issubclass(AudioContent, ContentBlock)
    assert issubclass(ResourceLink, ContentBlock)
    assert issubclass(EmbeddedResource, ContentBlock)


def test_blocks_stay_slotted():
    """Concrete blocks keep `__slots__` so they never regain a per-instance dict."""
    assert not hasattr(TextContent("x"), "__dict__")
    assert not hasattr(ImageContent("d", "image/png"), "__dict__")
    assert not hasattr(AudioContent("d", "audio/wav"), "__dict__")
    assert not hasattr(ResourceLink("res://a", "a"), "__dict__")
    assert not hasattr(EmbeddedResource({"uri": "res://a"}), "__dict__")


def test_a_content_block_subclass_without_slots_is_rejected():
    """A subclass that forgets `__slots__` fails loudly at class creation."""
    with pytest.raises(TypeError, match="must declare __slots__"):

        class _Bad(ContentBlock):
            pass
