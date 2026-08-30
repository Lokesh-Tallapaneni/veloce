"""URL maps — read-only view objects for route-table introspection.

`URLRule` is one registered rule (`rule`, `methods`, `endpoint`); `_URLMap`
wraps the app's route table, yielding `URLRule`s grouped per unique route and
caching the build until a route mutation drops the instance. Public surface:
`URLRule` is re-exported from `veloce.app`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce


class URLRule:
    """A single registered URL rule view object.

    Iterable over its fields as `(rule, methods, endpoint)` so callers that just
    want tuple-unpack semantics work; the same three are available as attributes
    for introspection. Slotted, so there are no others - read anything further
    off the route table itself.
    """

    __slots__ = ("rule", "methods", "endpoint")

    def __init__(self, rule: str, methods: list[str], endpoint: str) -> None:
        self.rule = rule
        self.methods = methods
        self.endpoint = endpoint

    def __iter__(self) -> Iterator[str | list[str]]:
        return iter((self.rule, self.methods, self.endpoint))

    def __repr__(self) -> str:
        return f"<URLRule {self.endpoint}: {','.join(self.methods)} {self.rule}>"


class _URLMap:
    """Veloce's read-only `Map`-style route-table wrapper.

    Iterating yields `URLRule` objects in registration order (grouped
    by `(path, name)` so each unique route is one rule even when
    several HTTP methods share it). `len()` counts unique rules.
    Lookup by endpoint name returns the list of matching rules.
    """

    __slots__ = ("_app", "_cached", "_by_endpoint")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._cached: list[URLRule] | None = None
        # Endpoint-name -> its rules, built alongside `_cached` so subscript is
        # not a full scan. Lives and dies with this instance (see `_build`).
        self._by_endpoint: dict[str, list[URLRule]] | None = None

    def _build(self) -> list[URLRule]:
        # Collect every (method, path, info) tuple, then group by
        # (path, endpoint-name) so a route registered for both GET and
        # POST shows up as a single rule. Result is cached on the
        # `_URLMap` instance; the app drops the whole instance via
        # `_invalidate_route_caches()` on any route mutation, so the
        # cache cannot go stale.
        cached = self._cached
        if cached is not None:
            return cached
        groups: dict[tuple[str, str], URLRule] = {}
        for method, path, info in self._app._collect_all_routes():
            key = (path, info.name)
            existing = groups.get(key)
            if existing is None:
                groups[key] = URLRule(rule=path, methods=[method], endpoint=info.name)
            else:
                existing.methods.append(method)
        result = list(groups.values())
        by_endpoint: dict[str, list[URLRule]] = {}
        for rule in result:
            by_endpoint.setdefault(rule.endpoint, []).append(rule)
        self._cached = result
        self._by_endpoint = by_endpoint
        return result

    def __iter__(self) -> Iterator[URLRule]:
        return iter(self._build())

    def __len__(self) -> int:
        return len(self._build())

    def __getitem__(self, endpoint: str) -> list[URLRule]:
        self._build()
        index = self._by_endpoint
        # Return a fresh list so a caller mutating it cannot corrupt the index.
        return list(index.get(endpoint, ())) if index is not None else []

    def __repr__(self) -> str:
        rules = self._build()
        return f"<URLMap with {len(rules)} rule{'s' if len(rules) != 1 else ''}>"
