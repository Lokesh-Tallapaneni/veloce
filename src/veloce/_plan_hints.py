"""Handler signature inspection and annotation resolution, salvaging unresolved names."""

from __future__ import annotations

import builtins
import contextlib
import inspect
import typing
import warnings
import weakref
from collections.abc import Callable, Mapping
from typing import Any, get_type_hints

import typing_extensions

from veloce._internal import resolve_hint_target
from veloce._params import (
    ParamBase,
    field_info_declares_constraints,
)
from veloce._plan_annotations import _callable_name, _unwrap_optional, extract_annotated_marker
from veloce._plan_kinds import _NO_PATH_PARAMS
from veloce._warnings import _RegistrationWarning

#: Memoised per handler; a caller mutating `__annotations__` needs a fresh one.
_INSPECT_CACHE: weakref.WeakKeyDictionary[
    Callable[..., Any], tuple[inspect.Signature, dict[str, Any]]
] = weakref.WeakKeyDictionary()


def _inspect_handler(
    handler: Callable[..., Any],
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Return `handler`'s signature and resolved type hints, or `None`.

    `None` means the callable has no inspectable signature - a builtin, or a C
    function. The two halves must describe the same object, and they do not
    follow the same indirection to find it (`get_type_hints` returns a class's
    *class-level* annotations), so the resolution is done by hand here first.
    PEP 593 metadata is kept, so a `Depends()` marker is detectable without the
    default-value form.
    """
    try:
        cached = _INSPECT_CACHE.get(handler)
    except TypeError:
        # An unhashable or non-weak-referenceable callable raises on the key.
        cached = None
    if cached is not None:
        return cached

    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None

    hint_target: Any = resolve_hint_target(handler)

    try:
        hints = get_type_hints(hint_target, include_extras=True)
    except Exception as exc:  # noqa: BLE001 - one unresolvable annotation must not drop a Depends() marker
        # `get_type_hints` is all-or-none, so one unresolvable annotation would
        # take a `Depends()` marker with it. Not cached: it varies by route.
        return sig, _salvage_hints(hint_target, exc, path_params, sig.parameters)

    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _INSPECT_CACHE[handler] = result
    return result


#: Parameters the plan binds from the name alone, plus the return annotation.
_BOUND_BY_NAME = frozenset({"request", "ws", "websocket", "return"})


def _salvage_hints(
    hint_target: Any,
    exc: Exception,
    path_params: frozenset[str],
    parameters: Mapping[str, inspect.Parameter],
) -> dict[str, Any]:
    """Resolve each annotation on its own, keeping the ones that succeed.

    Each goes through the same `get_type_hints` against the target's own
    globals, so a salvaged hint matches what the intact path would produce.
    """
    annotations = getattr(hint_target, "__annotations__", None) or {}
    globalns = getattr(hint_target, "__globals__", None)
    resolved: dict[str, Any] = {}
    unresolved: list[str] = []
    for name, annotation in annotations.items():
        probe = _AnnotationProbe()
        probe.__annotations__ = {name: annotation}
        try:
            resolved.update(get_type_hints(probe, globalns, include_extras=True))
        except Exception:  # noqa: BLE001 - this one genuinely cannot be resolved
            unresolved.append(name)
    # A parameter bound by NAME, and the return annotation, lose nothing here.
    consequential = [name for name in unresolved if name not in _BOUND_BY_NAME]
    if consequential:
        # `build_plan`'s signature: a partial's pre-bound parameter gets no slot.
        fatal = [
            name
            for name in consequential
            if _declaration_is_load_bearing(
                hint_target, annotations.get(name), name, parameters, path_params
            )
        ]
        if fatal:
            raise _unresolved_declaration_error(hint_target, fatal, exc)
        _warn_unresolved_annotations(hint_target, consequential, exc)
    return resolved


class _UnresolvedName:
    """Stands in for a name an annotation refers to that does not exist.

    Subscriptable and callable so the rest of the annotation still evaluates
    around it; the marker beside the unresolvable name is what matters.
    """

    __slots__ = ("name", "__metadata__")

    def __init__(self, name: str, metadata: tuple[Any, ...] = ()) -> None:
        self.name = name
        # Mirrors `Annotated`'s own attribute so the metadata scan reads one shape.
        self.__metadata__ = metadata

    def __getitem__(self, item: Any) -> _UnresolvedName:
        # The tail keeps the marker when `Annotated` is itself the missing name.
        tail = item[1:] if isinstance(item, tuple) else ()
        return _UnresolvedName(self.name, tail)

    def __call__(self, *args: Any, **kwargs: Any) -> _UnresolvedName:
        return self

    def __or__(self, other: Any) -> _UnresolvedName:
        # `X | None` must reach the same verdict as `Optional[X]`, not raise.
        return self

    __ror__ = __or__

    def __repr__(self) -> str:
        return f"<unresolved {self.name}>"


# A name moved under TYPE_CHECKING is most often a typing one, so resolving it
# lets the metadata scan judge it rather than refuse it unidentified.
_TYPING_NAMES: dict[str, Any] = {**vars(typing), **vars(typing_extensions)}


class _PlaceholderNamespace(dict[str, Any]):
    """A name mapping that yields a placeholder rather than raising `NameError`."""

    def __missing__(self, key: str) -> _UnresolvedName:
        return _UnresolvedName(key)


def _annotation_markers(hint_target: Any, annotation: Any) -> tuple[Any, tuple[Any, ...], bool]:
    """Return `(marker, metadata, known)` for an annotation that would not resolve.

    A string annotation is evaluated against the target's own globals with
    unresolvable names standing in as placeholders, so the marker beside the
    broken name is recovered as the object it is. Reading it by identity rather
    than by the text the author typed is what closes the rule: a scan for
    `"Security("` would miss `from veloce import Security as Guard`.

    `known` is False when the annotation could not be evaluated at all, and the
    caller refuses on that - not knowing is not a reason to admit.
    """
    live = annotation
    if isinstance(annotation, str):
        namespace = _PlaceholderNamespace(vars(builtins))
        namespace.update(_TYPING_NAMES)
        # The target's own globals are applied last so they always win.
        namespace.update(getattr(hint_target, "__globals__", None) or {})
        try:
            live = eval(annotation, {}, namespace)
        except Exception:  # noqa: BLE001 - an annotation that will not evaluate tells us nothing
            return None, (), False

    marker, _base = extract_annotated_marker(live)
    carrier = live
    if carrier is not None and not hasattr(carrier, "__metadata__"):
        was_optional, inner = _unwrap_optional(carrier)
        if was_optional:
            carrier = inner
    return marker, tuple(getattr(carrier, "__metadata__", ())), True


def _declaration_is_load_bearing(
    hint_target: Any,
    annotation: Any,
    name: str,
    parameters: Any,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> bool:
    """Whether dropping this unresolved annotation would change what runs.

    Two properties fail closed, under one rule: the declaration decides where
    the value comes from, or what executes, so discarding it hands that decision
    to the caller. A dropped `Depends` / `Security` means the route answers
    without running the dependency; a dropped `Header()` / `Cookie()` makes a
    credential readable from the query string, where the caller supplies it and
    the access log records it.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    marker, metadata, known = _annotation_markers(hint_target, annotation)
    if not known:
        return True
    if marker is not None:
        return True
    # A placeholder in a metadata slot means the marker's own name did not
    # resolve, and not knowing is no reason to serve.
    if any(isinstance(item, (Depends, ParamBase)) for item in metadata):
        return True
    if any(isinstance(item, _UnresolvedName) for item in metadata):
        return True
    # The fold carries a `Field`'s bound to resolve time, so the bound goes too.
    if any(field_info_declares_constraints(item) for item in metadata):
        return True
    if name in path_params:
        # Read from `path_params` first, so the declared source is unchanged.
        return False
    parameter = parameters.get(name)
    return parameter is not None and parameter.default is inspect.Parameter.empty


def _unresolved_declaration_error(hint_target: Any, fatal: list[str], exc: Exception) -> TypeError:
    """Build the error for an unresolved load-bearing annotation.

    Usually raised at route registration, but plans are also built on first use,
    so an override with a broken annotation raises here mid-request; the wording
    stays neutral about which.
    """
    where = _callable_name(hint_target)
    named = ", ".join(repr(n) for n in fatal)
    return TypeError(
        f"{where}: could not resolve the annotation on {named} "
        f"({type(exc).__name__}: {exc}). It is refused rather than dropped: "
        "a dropped `Depends()` / `Security()` marker "
        "answers the request without running the dependency, a dropped "
        "`Header()` / `Cookie()` marker moves the value to the query string "
        "where the caller supplies it, and a parameter with no default becomes "
        "a required query parameter. Import the name at runtime rather than "
        "only under TYPE_CHECKING."
    )


class _AnnotationProbe:
    """A throwaway carrier so one annotation can be resolved by itself.

    `get_type_hints` needs an object with `__annotations__`.
    """

    __slots__ = ("__annotations__",)


def _warn_unresolved_annotations(hint_target: Any, unresolved: list[str], exc: Exception) -> None:
    """Warn that some annotations could not be resolved, and nothing was lost.

    Only an annotation whose loss changes nothing reaches here; a load-bearing
    one raises in `_salvage_hints` instead.
    """
    where = _callable_name(hint_target)
    warnings.warn(
        f"{where}: could not resolve the annotation on "
        f"{', '.join(repr(n) for n in unresolved)} ({type(exc).__name__}: {exc}); "
        "those parameters are treated as unannotated. Nothing about where their "
        "values come from changes - import the name at runtime rather than only "
        "under TYPE_CHECKING to keep the annotation",
        _RegistrationWarning,
        stacklevel=3,
    )
