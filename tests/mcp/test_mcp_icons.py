"""MCP `Icon` value object and the descriptor-icon emission helpers."""

from __future__ import annotations

import dataclasses

import pytest

from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.icons import Icon, coerce_icons, render_icons


def test_icon_payload_carries_only_set_fields():
    """An icon with just a src omits the optional mimeType/sizes keys."""
    assert Icon("https://x/i.png").to_payload() == {"src": "https://x/i.png"}


def test_icon_payload_includes_optionals_when_set():
    """An icon renders mimeType and sizes when they are provided."""
    icon = Icon("https://x/i.png", mime_type="image/png", sizes=("48x48", "96x96"))
    assert icon.to_payload() == {
        "src": "https://x/i.png",
        "mimeType": "image/png",
        "sizes": ["48x48", "96x96"],
    }


def test_icon_is_immutable_and_slotted():
    """`Icon` is a frozen slotted value object."""
    icon = Icon("https://x/i.png")
    assert not hasattr(icon, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        icon.src = "https://x/other.png"


def test_coerce_icons_normalises_none_and_empty_to_empty_tuple():
    """A primitive without icons stays icon-free regardless of None vs []."""
    assert coerce_icons(None) == ()
    assert coerce_icons([]) == ()


def test_coerce_icons_keeps_supplied_icons():
    """A supplied icon sequence becomes a stored tuple."""
    icon = Icon("https://x/i.png")
    assert coerce_icons([icon]) == (icon,)


def test_render_icons_returns_none_when_empty():
    """A descriptor without icons renders no `icons` array."""
    assert render_icons(()) is None


def test_render_icons_renders_each_icon():
    """Each stored icon is rendered to its wire dict."""
    icons = (Icon("a"), Icon("b", mime_type="image/svg+xml"))
    assert render_icons(icons) == [{"src": "a"}, {"src": "b", "mimeType": "image/svg+xml"}]


def test_descriptor_icons_default_to_empty_tuple():
    """The shared `icons` field lives on the base, defaulting to the empty tuple."""
    assert MCPDescriptor(name="n", description="d").icons == ()


def test_descriptor_carries_supplied_icons():
    """A descriptor records the icons it was constructed with."""
    icon = Icon("https://x/i.png")
    descriptor = MCPDescriptor(name="n", description="d", icons=(icon,))
    assert descriptor.icons == (icon,)
