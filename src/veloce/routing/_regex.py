"""The regex fallback, for paths the radix tree cannot express.

A greedy converter followed by a static suffix has no tree form.
"""

from __future__ import annotations

import re

from veloce.routing.converters import (
    Converter,
    _iter_placeholders,
    extract_regex_converters,
)
from veloce.routing.route_info import RouteInfo


def _openapi_path_from_template(template: str) -> str:
    """Reduce a brace template to its OpenAPI path form (`{name}` per param).

    Balance-aware, so a spec carrying its own braces (`{id:[0-9]{2}}`) reduces
    cleanly to `{id}`.
    """
    out: list[str] = []
    pos = 0
    for ph in _iter_placeholders(template):
        out.append(template[pos : ph.start])
        out.append("{" + ph.name + "}")
        pos = ph.end
    out.append(template[pos:])
    return "".join(out)


class RegexRoute:
    """A route the radix tree cannot express, matched by a compiled regex.

    Consulted only on a tree miss, so the fast path never touches these.
    """

    __slots__ = ("pattern", "template", "param_names", "handlers", "converters", "tolerant_slash")

    def __init__(self, template: str, pattern: re.Pattern[str], param_names: list[str]) -> None:
        # Kept for `url_for` reverse resolution and OpenAPI path emission.
        self.template = template
        self.pattern = pattern
        self.param_names = param_names
        # Mirrors RadixNode.handlers, so both paths return the same shape.
        self.handlers: dict[str, RouteInfo] = {}
        # Coerces matched groups to the types the tree produces (`{n:int}` ->
        # int, not "3"). Bare and raw-regex groups are absent.
        self.converters: dict[str, Converter] = extract_regex_converters(template)
        # Set by `strict_slashes=False` to accept the extra trailing slash.
        self.tolerant_slash = False

    @property
    def openapi_path(self) -> str:
        """OpenAPI-style path string built from the template (`/users/{id}`)."""
        return _openapi_path_from_template(self.template)
