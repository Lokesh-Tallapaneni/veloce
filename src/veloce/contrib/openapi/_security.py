"""Security requirements — scheme definitions and per-operation requirement collection."""

from __future__ import annotations

import collections.abc
import warnings
from typing import Any

from veloce._params import ParamBase
from veloce.contrib._jsonschema import (
    _is_model_type,
)

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib.openapi._introspection import _handler_intro, _param_marker
from veloce.dependency import Depends
from veloce.security.base import SecurityScheme


def _scheme_definition(scheme: Any) -> tuple[str, dict[str, Any]] | None:
    """Return `(name, OpenAPI security scheme object)` for a Security() target.

    The scheme describes itself through `SecurityScheme.openapi_scheme`, so one
    defined outside this package is published like a built-in. `None` means
    "cannot be described", which the caller reports rather than passing off as
    an unguarded route. The name is the scheme's class name, so repeated
    registrations share one `components.securitySchemes` entry.
    """
    describe = getattr(scheme, "openapi_scheme", None)
    if describe is None:
        return None
    definition = describe()
    if not definition:
        return None
    return type(scheme).__name__, definition


def _dependency_params(target: Any) -> collections.abc.Iterator[tuple[Any, Any, Any]]:
    """Yield `(parameter, marker, annotation)` for each parameter of `target`.

    The descent step both dependency-graph walkers take, shared so they cannot
    disagree about what the graph contains. The annotation is the resolved one,
    falling back to the signature's. Yields nothing for an opaque target.
    """
    sig, hints = _handler_intro(target)
    if sig is None:
        return
    for param in sig.parameters.values():
        yield param, _param_marker(param, hints), hints.get(param.name, param.annotation)


def _collect_security_requirements(
    info: Any, registry: dict[str, dict[str, Any]]
) -> list[dict[str, list[str]]]:
    """Collect one OpenAPI security requirement per reachable `Security()`.

    Walks the route's dependency chain, mutating `registry` to add
    `components.securitySchemes` entries. Returns the operation-level
    `security` list — a sequence of `{schemeName: [scopes]}` dicts, empty
    when no `Security()` is reachable.
    """
    requirements: list[dict[str, list[str]]] = []
    seen: set[int] = set()

    def visit(dep: Any) -> None:
        # Either a Depends/Security marker, or a possibly-known scheme callable.
        if isinstance(dep, Depends):
            target = dep.dependency
            scheme_def = _scheme_definition(target)
            if scheme_def is not None:
                name, definition = scheme_def
                registry.setdefault(name, definition)
                # Scopes only matter for OAuth2, and only `Security()` carries them.
                scopes = list(getattr(dep, "scopes", []) or [])
                requirements.append({name: scopes})
                # A known scheme's internals are implementation, not policy.
                return
            if isinstance(target, SecurityScheme):
                # It guards the route but cannot say how, and publishing nothing
                # would assert the route is open to every reader of the document.
                warnings.warn(
                    f"{type(target).__name__} guards a route but does not implement "
                    "openapi_scheme(), so the route is published with no security "
                    "requirement - readers of the schema will see it as unauthenticated. "
                    "Return an OpenAPI Security Scheme Object from openapi_scheme().",
                    stacklevel=2,
                )
                return
            # Generic dep - recurse into its handler signature.
            inner = target
        else:
            inner = dep

        if inner is None or id(inner) in seen:
            return
        seen.add(id(inner))

        for _param, default, _annotation in _dependency_params(inner):
            if isinstance(default, Depends):
                visit(default)

    # Route-level dependencies (the `dependencies=[Depends(...)]` kwarg).
    for d in info.dependencies or ():
        visit(d)
    # Plus anything in the handler's own parameter defaults.
    for _param, default, _annotation in _dependency_params(info.handler):
        if isinstance(default, Depends):
            visit(default)
    return requirements


# ── Operation + webhook assembly ───────────────────────────


def _dependency_graph_has_validatable(info: Any) -> bool:
    """Return True if any dependency in the route's graph consumes validated input.

    The resolver raises `RequestValidationError` (422) for a Query/Path/Header/
    Cookie/Form/Body marker or a body-model parameter anywhere in the dependency
    graph - not only at the top level - so the auto-generated 422 response must
    reflect sub-dependency validation too. Known security schemes are not
    request-validated user input, so the walk does not descend into them.
    """
    seen: set[int] = set()

    def visit(dep_callable: Any) -> bool:
        if dep_callable is None or id(dep_callable) in seen:
            return False
        seen.add(id(dep_callable))
        for _param, default, annotation in _dependency_params(dep_callable):
            if isinstance(default, Depends):
                target = default.dependency
                if _scheme_definition(target) is not None:
                    continue
                if visit(target):
                    return True
                continue
            if isinstance(default, ParamBase):
                return True
            if _is_model_type(annotation):
                return True
        return False

    for dep in info.dependencies or []:
        if isinstance(dep, Depends):
            target = dep.dependency
            if _scheme_definition(target) is None and visit(target):
                return True
    return visit(info.handler)
