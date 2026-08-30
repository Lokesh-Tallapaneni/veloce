"""The radix tree's node, and the order its children are tried in.

The tree layer of `routing/`. `_converter_sort_key` is here rather than beside
the router because it exists to order a node's dynamic children, which is a
property of the node.
"""

from __future__ import annotations

from veloce.routing.converters import Converter, StringConverter
from veloce.routing.route_info import RouteInfo

# Static and wildcard nodes carry no parameter, so their converter is never
# consulted. A shared instance keeps the slot non-optional - the same choice
# `param_name` makes with `""` - because a `| None` here reaches the match loop,
# where narrowing it would cost a check per param child per request.
_NO_CONVERTER = StringConverter()


def _converter_sort_key(node: RadixNode) -> int:
    """Order competing parameter segments, most restrictive first.

    The value comes from the converter's own `specificity`, not from a table
    here; `Converter.specificity` documents why it is declared there.

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
        # Static children are indexed by exact segment for O(1) match-time
        # lookup. Param and wildcard children are few; they stay in their
        # own small containers and are scanned only after a static miss.
        self.static_children: dict[str, RadixNode] = {}
        self.param_children: list[RadixNode] = []
        # O(1) registration-time lookup keyed by (param_name, converter_type,
        # constraint), where `constraint` is the parametrized spec text (e.g.
        # `int(min=1)`) or None for an unparametrized converter. The ordered
        # list above is still the source of truth at match time.
        self._param_index: dict[tuple[str, type, str | None], RadixNode] = {}
        self.wildcard_child: RadixNode | None = None
        # Method name (uppercase) -> RouteInfo.
        self.handlers: dict[str, RouteInfo] = {}
        # `""` rather than `None` for a static node, which has no name. The
        # union would otherwise reach the match loop, where the three
        # `params[pname]` sites would each need a suppression or a runtime
        # narrowing check per param child per request.
        self.param_name: str = ""
        self.is_param = False
        self.is_wildcard = False
        self.trailing_slash = False
        # `/foo` and `/foo/` collapse to the same node, so the two strictness
        # flags are tracked independently: `trailing_slash` records that the
        # slashed form was registered, `unslashed_variant` the unslashed form.
        # When both are set the node serves both shapes and neither strictness
        # gate fires - registering one form must not flip the other to a 404.
        self.unslashed_variant = False
        # When True, the slashed and unslashed forms both match without
        # redirect - set by `strict_slashes=False` on `add_route`.
        self.tolerant_slash = False
        # Converter applied at match time. `add_route` sets it on every param
        # node; static and wildcard nodes keep the placeholder and never read it.
        self.converter: Converter = _NO_CONVERTER
