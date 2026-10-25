"""The radix tree's node, and the order its children are tried in."""

from __future__ import annotations

from typing import Any

from veloce.routing.converters import Converter, StringConverter
from veloce.routing.route_info import RouteInfo

# Static and wildcard nodes never consult their converter; a shared instance
# keeps the slot non-optional, so the match loop needs no narrowing check.
_NO_CONVERTER = StringConverter()


def _converter_sort_key(node: RadixNode) -> int:
    """Order competing parameter segments, most restrictive first.

    Runs once per `add_route` at startup, never on the per-request match path.
    """
    return node.converter.specificity


class RadixNode:
    """A node in the radix tree."""

    __slots__ = (
        "segment",
        "static_children",
        "param_children",
        "_param_index",
        "wildcard_child",
        "handlers",
        "param_name",
        "is_param",
        "is_wildcard",
        "trailing_slash",
        "unslashed_variant",
        "tolerant_slash",
        "converter",
    )

    def __init__(self, segment: str = "") -> None:
        self.segment = segment
        # O(1) match-time lookup; param and wildcard children are few and are
        # scanned only after a static miss.
        self.static_children: dict[str, RadixNode] = {}
        self.param_children: list[RadixNode] = []
        # Registration-time lookup only; the ordered list above stays the
        # source of truth at match time. `constraint` is the spec text or None.
        self._param_index: dict[tuple[str, type[Any], str | None], RadixNode] = {}
        self.wildcard_child: RadixNode | None = None
        # Method name (uppercase) -> RouteInfo.
        self.handlers: dict[str, RouteInfo] = {}
        # `""` rather than `None`: the union would reach the match loop, where
        # each `params[pname]` site would need a per-request narrowing check.
        self.param_name: str = ""
        self.is_param = False
        self.is_wildcard = False
        self.trailing_slash = False
        # `/foo` and `/foo/` collapse to one node, so each registered form is
        # flagged separately - registering one must not turn the other into a 404.
        self.unslashed_variant = False
        # Both forms match without redirect; set by `strict_slashes=False`.
        self.tolerant_slash = False
        self.converter: Converter = _NO_CONVERTER
