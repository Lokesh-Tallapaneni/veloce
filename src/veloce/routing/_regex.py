"""The regex fallback, for paths the radix tree cannot express.

A greedy converter followed by a static suffix has no tree form, so those routes
match here instead. `_openapi_path_from_template` is here because the regex
route is what holds the template it rewrites.
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

    Strips the `:converter` (or raw `:regex`) portion of every placeholder so
    `/users/{id:[0-9]+}` becomes `/users/{id}`. Balance-aware so a spec with
    its own braces (`{id:[0-9]{2}}`) reduces cleanly to `{id}`.
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

    Registered alongside the radix tree but consulted only on a tree miss
    (and only when regex routes exist). The fast path never touches these.
    """

    __slots__ = ("pattern", "template", "param_names", "handlers", "converters", "tolerant_slash")

    def __init__(self, template: str, pattern: re.Pattern[str], param_names: list[str]) -> None:
        # The original brace template (`/users/{id:[0-9]+}`), kept for
        # `url_for` reverse resolution and OpenAPI path emission.
        self.template = template
        self.pattern = pattern
        self.param_names = param_names
        # Method -> RouteInfo, mirroring RadixNode.handlers so the regex
        # path returns the same shape as the tree path.
        self.handlers: dict[str, RouteInfo] = {}
        # Built-in converter per placeholder name, so matched groups are
        # coerced to the same Python types the radix tree produces
        # (`{n:int}` -> int, not "3"). Bare and raw-regex groups are absent.
        self.converters: dict[str, Converter] = extract_regex_converters(template)
        # Mirrors `RadixNode.tolerant_slash` - set by `strict_slashes=False`
        # so a regex route accepts the missing/extra trailing slash too.
        self.tolerant_slash = False

    @property
    def openapi_path(self) -> str:
        """OpenAPI-style path string built from the template (`/users/{id}`)."""
        return _openapi_path_from_template(self.template)
