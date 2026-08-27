"""OpenAPI 3.1 schema generation and Swagger UI — auto-generated from routes."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import dataclasses
import datetime
import enum
import functools
import hashlib
import inspect
import logging
import pathlib
import types
import uuid
import warnings
import weakref
from decimal import Decimal
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, create_model

from veloce._constants import MIME_FORM_URLENCODED, MIME_JSON, MIME_MULTIPART_FORM_DATA
from veloce._handler_plan import extract_annotated_marker
from veloce._model_backend import (
    _msgspec,
    adapter_for,
    is_adaptable_model,
    is_msgspec_struct,
    is_pydantic_model,
)
from veloce._params import ParamBase
from veloce._protocol_constants import HTTP_METHOD_QUERY
from veloce._route_contract import RouteContract, iter_param_descriptors
from veloce.dependency import Depends
from veloce.routing.converters import path_param_schemas
from veloce.security.base import SecurityScheme
from veloce.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_ENTITY

_logger = logging.getLogger(__name__)

# Per-handler memoization of `inspect.signature` + `get_type_hints`. The
# OpenAPI generator visits each handler from four sites (operation
# parameters, webhook bodies, dependency-graph walk, dependency leaves),
# so introspecting once and reusing the result eliminates redundant
# work on every schema rebuild. `WeakKeyDictionary` so test suites and
# hot-reload sessions don't pin handlers for the process lifetime.
_HANDLER_INTRO_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)

# ── Introspection / merge helpers ──────────────────────────


def _group_field_schema(model: Any, wire_name: str) -> dict[str, Any] | None:
    """Return one field's declared schema from a grouped model, or `None`.

    `None` when the field resolves to a `$ref` (a nested model), which a
    parameter schema cannot carry - the caller falls back to the annotation.
    That fallback is lossy: the caller rebuilds the schema from the annotation
    alone, so the field's own `ge` / `le` / `title` do not reach the document
    while the resolver goes on enforcing them. A `$ref` is the expected reason
    to take it; an introspection failure is not, and is reported rather than
    left to look like the ordinary case.
    """
    try:
        properties = _grouped_model_properties(model)
    except Exception as exc:
        _warn_schema_fallback(f"grouped field {wire_name!r} on {model!r}", exc)
        return None
    prop = properties.get(wire_name)
    if prop is None or "$ref" in prop:
        return None
    return dict(prop)


@functools.lru_cache(maxsize=256)
def _grouped_model_properties(model: Any) -> dict[str, Any]:
    """`{wire name: schema}` for a grouped model, by alias where one is set."""
    schema = model.model_json_schema(by_alias=True)
    return schema.get("properties") or {}


def _handler_intro(handler: Any) -> tuple[Any, dict[str, Any]]:
    """Return `(signature, hints)` for `handler`, memoized per callable.

    `signature` is `None` when `inspect.signature` cannot introspect
    the handler (built-ins, `functools.partial` chains with non-callable
    `func`). `hints` falls back to `{}` on the same failure modes.
    """
    try:
        cached = _HANDLER_INTRO_CACHE.get(handler)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        sig = None
    hints: dict[str, Any] = {}
    if hasattr(handler, "__annotations__"):
        try:
            # `include_extras=True` keeps PEP 593 metadata: an
            # `Annotated[..., Security(...)]` parameter carries its marker there
            # and nowhere else, and stripping it published the route as
            # unauthenticated while the runtime still enforced it.
            hints = get_type_hints(handler, include_extras=True)
        except Exception:
            # `get_type_hints` raises a wide range (NameError on unresolved
            # forward refs, TypeError on bad annotations, recursion errors on
            # cyclic models); schema generation degrades gracefully to no hints
            # rather than failing the whole `/docs` build over one handler.
            hints = {}
    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _HANDLER_INTRO_CACHE[handler] = result
    return result


def _param_marker(param: Any, hints: dict[str, Any]) -> Any:
    """Find the `Depends` / parameter marker for `param`, however it was spelled.

    A marker reaches a handler two ways: as the parameter default
    (`cred = Security(scheme)`) or as PEP 593 metadata
    (`cred: Annotated[object, Security(scheme)]`). Reading only the default
    published an `Annotated`-spelled route as unauthenticated while the runtime
    enforced it - and `Annotated` is this project's documented house style, so
    the recommended form was the broken one.

    Resolution is delegated to the same helper the handler plan uses, so the
    published contract and the enforced one cannot drift apart again.
    """
    default = param.default
    if isinstance(default, (Depends, ParamBase)):
        return default
    marker, _base = extract_annotated_marker(hints.get(param.name, param.annotation))
    return marker if marker is not None else default


def _deep_merge(target: dict, overlay: dict) -> None:
    """Recursively merge `overlay` into `target` in place.

    Nested dicts merge key-by-key; any non-dict value (scalar, list)
    in `overlay` replaces the value in `target`.
    """
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ── Python type → JSON Schema helpers ──────────────────────


def _is_model_type(annotation: Any) -> bool:
    """Return True for a Pydantic ``BaseModel`` or a ``msgspec.Struct`` annotation.

    The single gate every request-body / response / list-item schema site uses,
    so both backends register a component schema and resolve to a ``$ref`` the
    same way.
    """
    if is_pydantic_model(annotation):
        return True
    return is_msgspec_struct(annotation)


def _literal_enum_schema(values: list) -> dict[str, Any]:
    """Build an OpenAPI schema for a fixed set of literal / enum values."""
    schema: dict[str, Any] = {"enum": values}
    if not values:
        return schema
    if all(isinstance(v, bool) for v in values):
        schema["type"] = "boolean"
    elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        schema["type"] = "integer"
    elif all(isinstance(v, str) for v in values):
        schema["type"] = "string"
    return schema


# Scalar Python types mapped to their fixed OpenAPI / JSON Schema fragment.
# Hoisted out of `_python_type_to_schema` so the table is built once at import
# time rather than per call. Callers mutate the schema they get back
# (`_apply_marker_constraints` writes minimum/maximum/pattern; the parameter
# builder sets `default`), so a lookup hit must return a fresh shallow copy -
# never a reference into this shared table. The copy is shallow: nested
# containers (the `list` entry's `items` dict) are shared across callers, so
# callers must mutate only top-level keys and never edit `schema["items"]` (or
# any nested object) in place, which would corrupt this table for every caller.
_SCALAR_TYPE_SCHEMAS: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    bytes: {"type": "string", "format": "byte"},
    list: {"type": "array", "items": {}},
    dict: {"type": "object"},
    datetime.datetime: {"type": "string", "format": "date-time"},
    datetime.date: {"type": "string", "format": "date"},
    datetime.time: {"type": "string", "format": "time"},
    datetime.timedelta: {"type": "string", "format": "duration"},
    uuid.UUID: {"type": "string", "format": "uuid"},
    Decimal: {"type": "number"},
    # A filesystem path arrives as a string. `path` is not a registered JSON
    # Schema format, but the keyword is an open annotation, so naming it tells a
    # client what the string means rather than leaving it indistinguishable from
    # free text. `PurePath` covers the platform-specific subclasses too.
    pathlib.PurePath: {"type": "string", "format": "path"},
    pathlib.Path: {"type": "string", "format": "path"},
}


def _python_type_to_schema(annotation: Any) -> dict:
    """Convert a Python type to its OpenAPI 3.1 / JSON Schema 2020-12 form.

    This builds the schema for a non-body parameter (query / path / header
    / cookie) or a form field. Those values arrive over the wire as raw
    strings: the resolver pulls them from `request.query_params`,
    `request.headers`, `request.cookies`, or `request.form()` and coerces
    each through `_coerce_value`. The schema therefore documents only what
    that string-origin pipeline can actually deliver.

    Scalars resolve to their richer JSON Schema form - `datetime`, `date`,
    `time`, `UUID`, `Decimal`, `Enum` subclasses and `Literal[...]` all
    keep their `format` / `enum` keywords. A nullable scalar
    (`Optional[T]`) unwraps to the inner schema.

    Pydantic models - and models nested inside `list` / `set` / `dict` -
    keep `{"type": "string"}`: the value arrives as a raw string and the
    resolver parses that string as a JSON document into the model
    (`?tag={"name":"x"}`), so the wire shape is genuinely a string.

    Multi-member unions are schema'd by which branches a *string* input
    can actually reach under Pydantic's smart coercion:

    - A union that includes `str` (`int | str`, `int | str | None`)
      collapses to `{"type": "string"}`: smart-mode coercion keeps a
      string value as the `str` member, so that is the only reachable
      branch.
    - A union with no string-accepting member (`int | float`,
      `UUID | int`, `date | datetime`) emits an `anyOf` over the members'
      schemas: the resolver feeds the string to Pydantic, which resolves
      it to whichever non-string branch matches, so several branches are
      genuinely reachable.
    """
    if annotation is None or annotation is inspect.Parameter.empty:
        return {"type": "string"}
    # `Any` means "any value allowed" - JSON Schema convention is an empty
    # schema, not a string default. (This does not reach a `dict` value type:
    # the `dict` branch below omits `additionalProperties` on purpose, for the
    # reason stated there.)
    if annotation is Any:
        return {}

    origin = get_origin(annotation)
    # Unwrap `Optional[T]` / `T | None` to the inner type so a nullable
    # rich-typed parameter still emits its `format` / `enum` keywords.
    #
    # For a genuine multi-member union the schema follows which branch a
    # string wire value can reach under Pydantic's smart coercion (verified
    # against the resolver, not assumed):
    #   - a member that accepts a string directly (`str` / `bytes`) always
    #     wins, so the union collapses to `{"type": "string"}`;
    #   - a Pydantic-model member is NOT reachable from a string in a union -
    #     the resolver only JSON-decodes a *bare* model annotation, so
    #     `Tag | int` rejects `?v={"name":"x"}` with 422. Model members are
    #     dropped from the union schema so it never advertises a 422 branch;
    #   - the remaining scalar branches (`int | float`, `UUID | int`, ...) are
    #     each genuinely reachable, so the union emits an `anyOf` over them.
    if origin is Union or origin is types.UnionType:
        members = get_args(annotation)
        inner = [a for a in members if a is not type(None)]
        if len(inner) == 1:
            return _python_type_to_schema(inner[0])
        if any(m is str or m is bytes for m in inner):
            return {"type": "string"}
        reachable = [m for m in inner if not _is_model_type(m)]
        if not reachable:
            # Every member is a model; none is reachable from a string in a
            # union (`A | B` 422s on any string value), so document a bare
            # object rather than a string the resolver would also reject.
            return {"type": "object"}
        if len(reachable) == 1:
            return _python_type_to_schema(reachable[0])
        return {"anyOf": [_python_type_to_schema(m) for m in reachable]}

    # Parametrised `list[T]` / `set[T]` -> an array schema with typed items.
    # A model item is not reachable: `list[Tag]` 422s on a JSON-array string,
    # so the item schema falls through to `{"type": "string"}` rather than the
    # model's fields (the resolver only JSON-decodes a bare model annotation).
    if origin in (list, set, tuple):
        args = get_args(annotation)
        item = _python_type_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item}
    # Parametrised `dict[K, V]` -> a bare object schema. A non-body dict
    # parameter is not wire-addressable at all: the resolver only JSON-decodes
    # a bare model annotation, so `dict[str, int]` (and `dict[str, Tag]`) 422s
    # on a JSON-object string and there is no repeated-param form for a dict.
    # Documenting typed `additionalProperties` would therefore advertise a
    # shape the resolver always rejects, so the value type is intentionally
    # not emitted.
    if origin is dict:
        return {"type": "object"}
    # `Literal["a", "b"]` -> an enum schema of the literal values.
    if origin is Literal:
        return _literal_enum_schema(list(get_args(annotation)))

    mapped = _SCALAR_TYPE_SCHEMAS.get(annotation)
    if mapped is not None:
        # Shallow copy: callers mutate the result (constraints, defaults).
        return dict(mapped)

    # `Enum` subclass -> an enum schema carrying the member values.
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _literal_enum_schema([member.value for member in annotation])

    # Pydantic model -> `string`. A non-body parameter / form field arrives
    # as a raw string; the resolver parses that string as a JSON document
    # and validates it into the model (`?tag={"name":"x"}`), so the wire
    # shape is a string. A model carried as a structured JSON body belongs
    # in `requestBody`, handled by `_pydantic_to_schema`.
    if is_pydantic_model(annotation):
        return {"type": "string"}

    return {"type": "string"}


# Placeholder `$ref` prefix written while the document is assembled. Every
# model reference points at `<PREFIX><token>` until `SchemaRegistry.finalize`
# assigns the final human-readable component names and rewrites the whole
# document in one pass. Keeping the prefix unmistakably non-OpenAPI guarantees
# a leftover placeholder (a builder bug) is easy to spot.
_REF_PLACEHOLDER_PREFIX = "#/$veloce-schema/"

# Pydantic emits nested-model references as `#/$defs/<Name>`. They are rewritten
# to point at the per-owner registry entry while the model's `$defs` are folded
# into the document, so this fragment is matched on the way in.
_PYDANTIC_DEF_PREFIX = "#/$defs/"


class SchemaRegistry:
    """Identity-keyed registry for the component schemas of one document.

    Models are keyed on the class object plus the JSON-Schema mode
    (`"validation"` for request bodies, `"serialization"` for responses), so
    two distinct classes that happen to share a ``__name__`` never overwrite
    each other and a model whose input and output shapes diverge can publish
    both. References handed back during assembly are placeholders; `finalize`
    resolves them to readable component names, disambiguating same-name
    collisions by the diverging module segment and collapsing a serialization
    variant back onto its validation schema when the two are byte-identical.
    """

    __slots__ = ("_entries", "_order", "separate_input_output")

    def __init__(self, separate_input_output: bool = True) -> None:
        # Identity key -> _SchemaEntry. The key is `(id(model), mode)`; the
        # entry retains the class itself so finalize can derive names.
        self._entries: dict[tuple[int, str], _SchemaEntry] = {}
        # Insertion order of identity keys, so `components.schemas` is emitted
        # deterministically in first-seen order rather than dict-hash order.
        self._order: list[tuple[int, str]] = []
        self.separate_input_output = separate_input_output

    def ref(self, model: type[BaseModel], mode: str = "validation") -> dict[str, str]:
        """Register `model` under `mode` and return a placeholder `$ref`."""
        # Serialization is only requested for response schemas. When the app
        # opts out of split schemas, fold every request back onto the single
        # validation variant so the document keeps one schema name per model.
        if mode == "serialization" and not self.separate_input_output:
            mode = "validation"
        key = (id(model), mode)
        entry = self._entries.get(key)
        if entry is None:
            entry = _SchemaEntry(model, mode)
            self._entries[key] = entry
            self._order.append(key)
        return {"$ref": f"{_REF_PLACEHOLDER_PREFIX}{entry.token}"}

    def finalize(self, document: dict[str, Any]) -> dict[str, dict]:
        """Resolve placeholders, rewrite `document` in place, return schemas.

        Assigns each entry a final component name (bare class name when that
        name is unique across the document, otherwise qualified by the module
        tail), folds a byte-identical serialization variant onto its validation
        twin, then rewrites every placeholder `$ref` reachable from `document`.
        """
        token_to_name: dict[str, str] = {}
        components: dict[str, dict] = {}

        # Group entries by the human-readable base name they want. A serialization
        # entry is suffixed `-Output` only when it actually diverges from its
        # validation twin, so the common (non-diverging) case keeps one name.
        wanted: dict[str, list[_SchemaEntry]] = {}
        for key in self._order:
            entry = self._entries[key]
            entry.build()
            base = entry.model.__name__
            if entry.mode == "serialization":
                val_key = (id(entry.model), "validation")
                twin = self._entries.get(val_key)
                if twin is not None:
                    twin.build()
                    # Compare the FULL schema - the top-level root (`body`) AND
                    # every nested `$defs` entry (`defs`). Two models can share an
                    # identical root while a nested model carries serialization-only
                    # fields (a `computed_field`, a read-only / serialization alias)
                    # that only surface in `mode="serialization"`. Folding on the
                    # root alone would drop that distinct `-Output` schema even with
                    # `separate_input_output_schemas=True`. Only collapse when the
                    # entire schema, nested defs included, is byte-identical.
                    if twin.body == entry.body and twin.defs == entry.defs:
                        # Output equals input: reuse the validation component
                        # rather than emit a redundant `-Output` schema.
                        entry.alias_token = twin.token
                        continue
                    # A model used for both input and output whose shapes
                    # diverge keeps the bare name for the request schema and a
                    # distinct `-Output` name for the response schema. With no
                    # validation twin (response-only model) the bare name is
                    # free, so no suffix is needed.
                    base = f"{base}-Output"
            wanted.setdefault(base, []).append(entry)

        for base, group in wanted.items():
            if len(group) == 1:
                token_to_name[group[0].token] = base
            else:
                # Same base name from >1 distinct class: qualify each by the
                # last segment of its defining module (`schemas.User` -> the
                # `User__schemas` form). Identical qualified names are made
                # unique with a numeric tail so the bijection always holds.
                used: dict[str, int] = {}
                for entry in group:
                    seg = (entry.model.__module__ or "").rsplit(".", 1)[-1]
                    candidate = f"{base}__{seg}" if seg else base
                    seen = used.get(candidate, 0)
                    used[candidate] = seen + 1
                    if seen:
                        candidate = f"{candidate}_{seen}"
                    token_to_name[entry.token] = candidate

        # Materialise components, folding each model's own `$defs` (nested
        # models) into the shared component map. A nested def is emitted under
        # its bare Pydantic name when free. When a later owner brings a def of
        # the SAME name but DIFFERENT content - a serialization-mode nested model
        # that diverges from its validation twin (a nested `computed_field`, a
        # read-only field) - it must not be dropped onto the first writer, or the
        # serialization-only nested field disappears from the response schema.
        # Divergence is transitive: a def whose own body matches the first
        # writer's but which references (directly or through other defs) a
        # diverging child still resolves to the WRONG subtree if folded, because
        # its surviving `#/$defs/<child>` ref points at the validation child. So
        # every def on a path that reaches a divergence gets its own `-Output`
        # variant, and the owner's body plus each variant's internal refs are
        # repointed at the renamed children so the response schema reaches the
        # serialization subtree end to end.
        for key in self._order:
            entry = self._entries[key]
            if entry.alias_token is not None:
                continue
            name = token_to_name[entry.token]
            components[name] = entry.body
            local_renames = self._diverging_def_renames(entry.defs, components)
            for def_name, def_schema in entry.defs.items():
                target = local_renames.get(def_name)
                if target is not None:
                    # Diverging (directly or transitively): emit under the unique
                    # name with its own refs repointed at any renamed children.
                    rewritten = _copy_with_local_defs(def_schema, local_renames)
                    components[target] = rewritten
                elif def_name not in components:
                    components[def_name] = def_schema
                # An identical, non-diverging re-emission is harmless: the first
                # writer stands.
            if local_renames:
                _rewrite_local_defs(entry.body, local_renames)

        # Rewrite every placeholder reference in the whole document, plus the
        # `#/$defs/...` references inside the freshly materialised components.
        # Resolve aliases through a token -> alias_token map built once, so the
        # rewrite is linear rather than O(n^2) over the entries per token.
        alias_by_token: dict[str, str] = {}
        for entry in self._entries.values():
            if entry.alias_token is not None:
                alias_by_token[entry.token] = entry.alias_token
        resolved_token = {
            t: (token_to_name.get(t) or token_to_name[alias_by_token.get(t, t)])
            for t in self._all_tokens()
        }
        _rewrite_refs(document, resolved_token)
        _rewrite_refs(components, resolved_token)
        return components

    def _diverging_def_renames(
        self, defs: dict[str, dict], components: dict[str, dict]
    ) -> dict[str, str]:
        """Map nested-def names needing an `-Output` variant to unique names.

        A def diverges when its own body differs from the committed component of
        the same name, OR when it references (transitively) a def that diverges.
        The second clause is the recursive part: a structurally-identical wrapper
        that points at a diverging child must still get its own variant so the
        response schema follows the serialization subtree rather than collapsing
        onto the validation child. Computed as a fixpoint over the `#/$defs/...`
        reference graph, then each diverging name is allocated a unique target.
        """
        diverging: set[str] = set()
        for def_name, def_schema in defs.items():
            existing = components.get(def_name)
            if existing is not None and existing != def_schema:
                diverging.add(def_name)

        # Propagate divergence backwards along references until stable: any def
        # that reaches a diverging def is itself diverging for the output graph.
        changed = True
        while changed:
            changed = False
            for def_name, def_schema in defs.items():
                if def_name in diverging:
                    continue
                for target in _local_def_refs(def_schema):
                    if target in diverging:
                        diverging.add(def_name)
                        changed = True
                        break

        renames: dict[str, str] = {}
        for def_name in defs:
            if def_name in diverging:
                renames[def_name] = self._unique_def_name(components, def_name)
        return renames

    @staticmethod
    def _unique_def_name(components: dict[str, dict], base: str) -> str:
        """Return a component name derived from `base` not already in use.

        The `-Output` suffix mirrors the top-level serialization variant naming
        so a diverging nested model reads as its owner's output twin; finding a
        free name from there is `_unique_component_name`'s job, which this used
        to restate with the arguments in the other order.
        """
        return _unique_component_name(f"{base}-Output", components)

    def _all_tokens(self) -> list[str]:
        return [self._entries[k].token for k in self._order]


def _iter_dicts(node: Any) -> collections.abc.Iterator[dict]:
    """Yield every mapping in a JSON tree, the outermost first.

    One walk for the several passes that visit a generated schema: rewriting a
    `binary` format, collecting local `$defs` targets, repointing renamed refs,
    resolving placeholder refs, and validating that every ref exists. Each was
    written as its own recursive function differing only in the body applied at
    each mapping.

    Iterative rather than recursive on purpose: a deeply nested schema - a model
    referring to itself through a long chain - would otherwise be bounded by the
    interpreter's recursion limit rather than by memory. Callers may reassign
    values on a yielded mapping (every pass below does), but must not add or
    remove keys while iterating.
    """
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _warn_schema_fallback(subject: str, exc: Exception) -> None:
    """Report a schema-generation failure, then let the caller fall back.

    Degrading silently to `{type: object}` hides real model bugs, so the failure
    is logged at WARNING and `/docs` still renders - an underspecified schema
    beats a 500.

    One message because there were four, and they had already drifted: the
    msgspec copy omitted the "inspect the model / attach a debugger" guidance
    the other three carried, so the operator least likely to guess the cause got
    the least help.
    """
    _logger.warning(
        "OpenAPI schema generation failed for %s: %s. "
        "Falling back to {type: object}. "
        "Inspect the model definition or attach a debugger to "
        "veloce.contrib.openapi to see the full traceback.",
        subject,
        exc,
        exc_info=_logger.isEnabledFor(logging.DEBUG),
    )


def _rewrite_byte_format(node: Any) -> None:
    """Rewrite bytes string fields from OpenAPI `format: binary` to `format: byte`.

    Veloce's JSON encoder base64-encodes `bytes`/`bytearray`, so a bytes field in
    a JSON model travels as a base64 string - RFC 4648 `byte` - not raw `binary`.
    Pydantic emits `binary` for `bytes`; this realigns the generated schema with
    the actual serialized form. Walks nested objects, arrays, and `$defs` in place.
    """
    for mapping in _iter_dicts(node):
        if mapping.get("type") == "string" and mapping.get("format") == "binary":
            mapping["format"] = "byte"


class _SchemaEntry:
    """One `(model, mode)` registration awaiting name assignment."""

    __slots__ = ("alias_token", "body", "defs", "mode", "model", "token")

    # Monotonic token source shared across entries so placeholder strings stay
    # short and globally unique within a process; only used as an opaque key.
    _counter = 0

    def __init__(self, model: type[BaseModel], mode: str) -> None:
        self.model = model
        self.mode = mode
        cls = type(self)
        cls._counter += 1
        self.token = str(cls._counter)
        self.body: dict[str, Any] = {}
        self.defs: dict[str, dict] = {}
        # Set when this entry collapses onto another (byte-identical output).
        self.alias_token: str | None = None

    def build(self) -> None:
        """Populate `body` / `defs` from the model's JSON Schema once."""
        if self.body:
            return
        if is_msgspec_struct(self.model):
            self._build_msgspec()
            return
        try:
            if is_adaptable_model(self.model):
                # A dataclass / TypedDict has no `model_json_schema`; its shape
                # comes from the same adapter that validates it, so the document
                # and the validator cannot describe different fields.
                schema = adapter_for(self.model).json_schema()
            else:
                schema = self.model.model_json_schema(mode=self.mode)  # type: ignore[arg-type]
        except Exception as exc:
            _warn_schema_fallback(f"{self.model.__name__} ({self.mode} mode)", exc)
            self.body = {"type": "object"}
            return
        # Realign bytes fields with veloce's base64 JSON encoding (binary -> byte).
        _rewrite_byte_format(schema)
        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                self.defs[def_name] = def_schema
            del schema["$defs"]
        self.body = schema

    def _build_msgspec(self) -> None:
        """Populate `body` / `defs` from a `msgspec.Struct`'s JSON Schema.

        msgspec has no separate validation / serialization shape, so `mode` is
        not consulted - the registry folds the byte-identical variants onto one
        component name. The `#/$defs/{name}` ref template matches the nested-ref
        prefix the document rewriter already repoints into `components.schemas`,
        so nested structs resolve with no extra translation.
        """
        try:
            schemas, components = _msgspec.json.schema_components(
                [self.model], ref_template="#/$defs/{name}"
            )
        except Exception as exc:
            _warn_schema_fallback(f"{self.model.__name__} (msgspec)", exc)
            self.body = {"type": "object"}
            return
        # msgspec returns the model's own schema inside `components` and a
        # top-level `$ref` to it; lift that into `body` and keep the rest as
        # nested `$defs`. A struct with no nested refs may come back inline.
        root = schemas[0]
        if isinstance(root, dict) and set(root) == {"$ref"}:
            name = root["$ref"].rsplit("/", 1)[-1]
            self.body = components.pop(name, {"type": "object"})
        else:
            self.body = dict(root)
        for def_name, def_schema in components.items():
            self.defs[def_name] = def_schema
        _rewrite_byte_format(self.body)
        for def_schema in self.defs.values():
            _rewrite_byte_format(def_schema)


def _local_def_refs(node: Any) -> set[str]:
    """Collect every `#/$defs/<name>` target referenced anywhere under `node`."""
    return {
        ref[len(_PYDANTIC_DEF_PREFIX) :]
        for mapping in _iter_dicts(node)
        if isinstance(ref := mapping.get("$ref"), str) and ref.startswith(_PYDANTIC_DEF_PREFIX)
    }


def _copy_with_local_defs(node: dict, renames: dict[str, str]) -> dict:
    """Deep-copy `node`, repointing renamed `#/$defs/<old>` refs to the new names.

    Used to emit a diverging nested def's `-Output` variant: the source schema is
    shared with the validation owner, so it must be copied before its internal
    references are redirected at the renamed (serialization) children.
    """
    copied = copy.deepcopy(node)
    _rewrite_local_defs(copied, renames)
    return copied


def _rewrite_local_defs(node: Any, renames: dict[str, str]) -> None:
    """Repoint `#/$defs/<old>` refs in `node` to `#/components/schemas/<new>`.

    Used for an owner whose diverging nested def was emitted under a
    disambiguated name; only the listed `$defs` names are rewritten so the
    owner reaches its distinct nested model while leaving every other ref for
    the global pass.
    """
    for mapping in _iter_dicts(node):
        ref = mapping.get("$ref")
        if isinstance(ref, str) and ref.startswith(_PYDANTIC_DEF_PREFIX):
            new = renames.get(ref[len(_PYDANTIC_DEF_PREFIX) :])
            if new is not None:
                mapping["$ref"] = f"#/components/schemas/{new}"


def _rewrite_refs(node: Any, token_to_name: dict[str, str]) -> None:
    """Rewrite placeholder and `#/$defs/...` `$ref`s in place throughout `node`."""
    for mapping in _iter_dicts(node):
        ref = mapping.get("$ref")
        if not isinstance(ref, str):
            continue
        if ref.startswith(_REF_PLACEHOLDER_PREFIX):
            name = token_to_name.get(ref[len(_REF_PLACEHOLDER_PREFIX) :])
            if name is not None:
                mapping["$ref"] = f"#/components/schemas/{name}"
        elif ref.startswith(_PYDANTIC_DEF_PREFIX):
            mapping["$ref"] = f"#/components/schemas/{ref[len(_PYDANTIC_DEF_PREFIX) :]}"


def _register_schema(name: str, schema: dict, registry: dict[str, dict]) -> None:
    """Hoist a generated schema's `$defs` into `registry` and record it by name."""
    _rewrite_byte_format(schema)
    if "$defs" in schema:
        for def_name, def_schema in schema["$defs"].items():
            registry[def_name] = def_schema
        del schema["$defs"]
    # A recursive model renders as `{"$defs": {Name: <real object>},
    # "$ref": ".../Name"}`: after extracting `$defs` the leftover top-level
    # schema is a bare self-`$ref`. Overwriting the registry entry with it
    # would clobber the real definition pulled from `$defs`, leaving an
    # unresolvable cycle. Keep the extracted def.
    if not (list(schema.keys()) == ["$ref"] and name in registry):
        registry[name] = schema


def _adapted_to_schema(model: Any, registry: dict[str, dict]) -> dict:
    """Convert a dataclass / `TypedDict` to a name-keyed `$ref`, extending `registry`.

    The adapted counterpart to `_pydantic_to_schema`: both hoist their `$defs`
    through `_register_schema`, so a nested or recursive shape resolves the same
    way whichever backend declared it.
    """
    name = getattr(model, "__name__", "Model")
    if name not in registry:
        try:
            _register_schema(name, adapter_for(model).json_schema(), registry)
        except Exception as exc:
            _warn_schema_fallback(name, exc)
            registry[name] = {"type": "object"}
    return {"$ref": f"#/components/schemas/{name}"}


def _pydantic_to_schema(
    model: type[BaseModel],
    registry: dict[str, dict],
    mode: str = "validation",
    by_alias: bool = True,
) -> dict:
    """Convert a Pydantic model to a name-keyed `$ref`, extending `registry`.

    Standalone renderer for callers that build a self-contained schema envelope
    (the MCP plan bridge inlines these into per-tool `$defs`). `mode` selects the
    JSON Schema variant: ``"validation"`` (the default, for request inputs) or
    ``"serialization"`` (for response/output schemas, so computed and
    serialization-only fields are documented as clients actually receive them).
    `by_alias` matches the property keys to how the value is dumped, so a caller
    that emits the value without aliases (``model_dump(by_alias=False)``) can
    request a field-name schema that the value conforms to. The document-wide
    OpenAPI generator uses `SchemaRegistry` instead, which keys on class identity
    and supports dual validation/serialization modes.
    """
    name = model.__name__
    if name not in registry:
        try:
            schema = model.model_json_schema(mode=mode, by_alias=by_alias)  # type: ignore[arg-type]
            _register_schema(name, schema, registry)
        except Exception as exc:
            _warn_schema_fallback(name, exc)
            registry[name] = {"type": "object"}
    return {"$ref": f"#/components/schemas/{name}"}


#: Origins that serialise to a JSON array. The docstring below has always
#: promised "any `Sequence[MyModel]`"; only `list` was handled, so a handler
#: annotated `-> Sequence[Item]` or `-> tuple[Item, ...]` lost its response
#: schema entirely.
_SEQUENCE_ORIGINS = (list, collections.abc.Sequence, tuple, set, frozenset, collections.abc.Set)


#: Derived models built for a route's `response_model_include` /
#: `response_model_exclude`, keyed by `(model, include, exclude)` so two routes
#: filtering a model the same way share one component rather than emitting two.
_FILTERED_MODELS: dict[tuple[int, frozenset | None, frozenset | None], Any] = {}

#: Longest joined field list to spell out in a derived component's name.
_FILTERED_NAME_LIMIT = 48


def _filtered_response_model(model: Any, include: set[str] | None, exclude: set[str] | None) -> Any:
    """Build a model carrying only the fields a route's include/exclude leaves.

    `response_model_include` / `response_model_exclude` filter what the route
    *sends*, and the lowering knew nothing about them - so a route sending
    `{"name": ...}` was documented as returning the whole model, with the fields
    it omits still marked `required`. A client generated from that document
    expects a field the route never sends.

    Deriving a model rather than editing a schema dict keeps every downstream
    behaviour - naming, `$defs` hoisting, nested refs, serialization mode - on
    the path the unfiltered model already takes.
    """
    if not include and not exclude:
        return model
    key = (
        id(model),
        frozenset(include) if include else None,
        frozenset(exclude) if exclude else None,
    )
    cached = _FILTERED_MODELS.get(key)
    if cached is not None:
        return cached
    fields: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if include is not None and name not in include:
            continue
        if exclude and name in exclude:
            continue
        fields[name] = (field.annotation, field)
    if not fields:
        # Every field filtered out: nothing to derive, and an empty object is a
        # less useful document than the model itself.
        return model
    # The name reaches the components section, so it has to be stable for a
    # given filter and distinct between filters. The surviving field names give
    # both, and read better than an opaque token; a wide model falls back to a
    # digest rather than emitting an unreadable component key.
    surviving = sorted(fields)
    suffix = "_".join(surviving)
    if len(suffix) > _FILTERED_NAME_LIMIT:
        suffix = hashlib.blake2b(suffix.encode(), digest_size=6).hexdigest()
    derived = create_model(f"{model.__name__}_{suffix}", **fields)
    _FILTERED_MODELS[key] = derived
    return derived


def _response_model_to_schema(
    response_model: Any,
    registry: SchemaRegistry,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> dict | None:
    """Render `response_model` into an OpenAPI schema object.

    Handles four shapes:
    - `MyModel` (Pydantic BaseModel subclass) -> `{"$ref": ".../MyModel"}`.
    - `list[MyModel]` (or any `Sequence[MyModel]`) -> array-of-refs.
    - `A | B` / `A | None` (a union of models) -> `oneOf`, with `None`
      rendered as the JSON Schema null type so an optional result is documented
      rather than dropped.
    - Anything else -> `None` (caller omits the schema).

    Response models render under the model's serialization JSON Schema so
    computed / read-only fields are documented as clients actually receive them.
    """
    origin = get_origin(response_model)
    if origin in _SEQUENCE_ORIGINS:
        args = get_args(response_model)
        # `tuple[Item, ...]` carries an Ellipsis in its second slot; every
        # sequence origin documents its element type from the first.
        if args and _is_model_type(args[0]):
            element = _filtered_response_model(args[0], include, exclude)
            return {"type": "array", "items": registry.ref(element, mode="serialization")}
        return {"type": "array", "items": {}}

    if origin in (Union, types.UnionType):
        variants: list[dict] = []
        for arg in get_args(response_model):
            if arg is type(None):
                variants.append({"type": "null"})
            elif _is_model_type(arg):
                variants.append(registry.ref(arg, mode="serialization"))
            else:
                # A member with no schema form makes the whole union
                # inexpressible; omit rather than document it partially.
                return None
        if not variants:
            return None
        return variants[0] if len(variants) == 1 else {"oneOf": variants}

    if _is_model_type(response_model):
        filtered = _filtered_response_model(response_model, include, exclude)
        return registry.ref(filtered, mode="serialization")

    return None


# ── Info / parameter / body / response builders ────────────


def _build_info_object(app: Any) -> dict[str, Any]:
    """Return the OpenAPI `info` object assembled from app metadata."""
    # Read directly: `Veloce.__init__` requires both to be non-empty strings, so
    # a fallback here is a second copy of a default that cannot be reached. The
    # two copies had already disagreed - this one said "Veloce API" where the
    # constructor and the MCP server said "Veloce", so one app named itself two
    # things across its two doors.
    info_obj: dict[str, Any] = {"title": app.title, "version": app.version}
    if app.summary:
        info_obj["summary"] = app.summary
    if app.description:
        info_obj["description"] = app.description
    if app.terms_of_service:
        info_obj["termsOfService"] = app.terms_of_service
    if app.contact:
        info_obj["contact"] = app.contact
    if app.license_info:
        info_obj["license"] = app.license_info
    return info_obj


def _apply_marker_constraints(param_schema: dict[str, Any], marker: Any) -> None:
    """Copy validation / metadata keywords from a `ParamBase` marker onto `param_schema`."""
    if getattr(marker, "title", None):
        param_schema["title"] = marker.title
    if marker.description:
        param_schema["description"] = marker.description
    if marker.ge is not None:
        param_schema["minimum"] = marker.ge
    if marker.le is not None:
        param_schema["maximum"] = marker.le
    # OpenAPI 3.1 / JSON Schema 2020-12: gt/lt map to the
    # numeric `exclusiveMinimum` / `exclusiveMaximum`.
    if marker.gt is not None:
        param_schema["exclusiveMinimum"] = marker.gt
    if marker.lt is not None:
        param_schema["exclusiveMaximum"] = marker.lt
    if marker.min_length is not None:
        param_schema["minLength"] = marker.min_length
    if marker.max_length is not None:
        param_schema["maxLength"] = marker.max_length
    if getattr(marker, "multiple_of", None) is not None:
        param_schema["multipleOf"] = marker.multiple_of
    if marker.regex is not None:
        param_schema["pattern"] = marker.regex
    # OpenAPI 3.1 / JSON Schema 2020-12 - `examples` is an array of
    # sample values on the schema object.
    if getattr(marker, "examples", None):
        param_schema["examples"] = list(marker.examples or [])


@dataclasses.dataclass(frozen=True, slots=True)
class _FormField:
    """One `Form()` / `File()` parameter, as the request body will describe it."""

    alias: str
    schema: dict
    required: bool
    is_file: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _BodyField:
    """One `Body(embed=True)` parameter - a named key of the JSON object body."""

    alias: str
    schema: dict
    required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _ScalarBody:
    """A non-embedded `Body()` over a non-model: the whole JSON body."""

    schema: dict
    required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _RouteParameters:
    """What lowering a route's handler plan says about its inputs.

    The fields were a 5-tuple whose meanings lived in prose, unpacked
    positionally at the single call site - so `form_fields[0][3]` was `is_file`
    only if you had read the docstring. Naming them puts that in the type.
    """

    parameters: list[dict]
    request_body_schema: dict | None
    form_fields: list[_FormField]
    body_fields: list[_BodyField]
    scalar_body: _ScalarBody | None


def _extract_parameters(info: Any, schemas_registry: SchemaRegistry) -> _RouteParameters:
    """Classify every parameter by lowering the route's handler plan.

    Each field of the returned `_RouteParameters` is documented on the record
    itself; `_extract_request_body` consumes the three body-shaped ones.

    Walks the same `HandlerPlan` the resolver executes (via
    `iter_param_descriptors`), so the documented contract matches the one the
    server enforces. Depends/Security and JSON `Body()` markers are not yielded
    as parameters - they belong to other parts of the operation object.
    """
    parameters: list[dict] = []
    request_body_schema: dict | None = None
    form_fields: list[_FormField] = []
    body_fields: list[_BodyField] = []
    scalar_body: _ScalarBody | None = None

    for d in iter_param_descriptors(RouteContract.from_route_info(info)):
        marker = d.marker
        # `include_in_schema=False` - resolved at runtime but omitted from docs.
        if marker is not None and not getattr(marker, "include_in_schema", True):
            continue

        location = d.location

        if location == "body":
            # A bare model or `Body()`-wrapped model is the JSON request body. A
            # JSON body is always required there: the resolver 422s on a missing
            # body even for an `Optional` model.
            if d.model is not None:
                request_body_schema = schemas_registry.ref(d.model, mode="validation")
                continue
            # A `Body()` over a non-model still has a documentable shape, and
            # which shape depends on `embed`: an embedded param is one named key
            # of a JSON object body, while a non-embedded one receives the whole
            # body. Both are collected here so the operation documents the body
            # the resolver actually reads.
            body_schema: dict[str, Any] = _python_type_to_schema(d.target_type)
            if d.is_list:
                body_schema = {"type": "array", "items": body_schema}
            if marker is not None:
                _apply_marker_constraints(body_schema, marker)
                body_required = not (marker.has_default or d.is_optional)
                body_alias = marker.alias or d.name
                embedded = bool(getattr(marker, "embed", False))
            else:
                body_required = not (d.has_default or d.is_optional)
                body_alias = d.name
                embedded = False
            if embedded:
                body_fields.append(_BodyField(body_alias, body_schema, body_required))
            elif scalar_body is None:
                scalar_body = _ScalarBody(body_schema, body_required)
            continue

        if location == "form":
            if d.is_file:
                field_schema: dict[str, Any] = {"type": "string", "format": "binary"}
            else:
                field_schema = _python_type_to_schema(d.target_type)
            if marker is not None:
                if marker.description:
                    field_schema["description"] = marker.description
                if getattr(marker, "title", None):
                    field_schema["title"] = marker.title
                field_required = not (marker.has_default or d.is_optional)
                field_alias = marker.alias or d.name
            else:
                # A bare `UploadFile`: optional when it carries a default or an
                # `Optional` annotation - the resolver leaves the kwarg unset and
                # the handler default applies, so the field is not required.
                field_required = not (d.has_default or d.is_optional)
                field_alias = d.name
            form_fields.append(_FormField(field_alias, field_schema, field_required, d.is_file))
            continue

        # path / query / header / cookie parameter.
        param_schema: dict[str, Any]
        if d.group_field and (grouped := _group_field_schema(d.model, d.wire_name)) is not None:
            # The field's own declaration owns its constraints; rebuilding the
            # schema from the annotation alone would publish a laxer contract
            # than the resolver enforces.
            param_schema = grouped
        elif d.is_list:
            param_schema = {"type": "array", "items": _python_type_to_schema(d.target_type)}
        else:
            param_schema = _python_type_to_schema(d.target_type)

        if marker is not None:
            _apply_marker_constraints(param_schema, marker)

        # Path parameters document the Python name; every other location honours
        # the alias / hyphenated wire name the resolver actually reads.
        param_alias = d.name if location == "path" else d.wire_name

        # A path parameter is always required - the route cannot match without
        # its segment (OpenAPI 3.1 requires `required: true`). For every other
        # location an `Optional[T]` annotation makes the value omittable even
        # with no default: the resolver binds `None` when it is absent, so the
        # documented contract matches the resolver only when `is_optional`
        # (or a default) relaxes required.
        if location == "path":
            required = True
        elif marker is not None:
            required = not (marker.has_default or d.is_optional)
            if marker.has_default and marker.default is not ...:
                default_val = marker.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val
        else:
            required = not (d.has_default or d.is_optional)
            if d.has_default:
                default_val = d.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val

        param_info: dict[str, Any] = {
            "name": param_alias,
            "in": location,
            "required": required,
            "schema": param_schema,
        }

        # OpenAPI 3.1 Sec. 4.8.12.1 - array-valued query parameters default
        # to `style: form`, `explode: true` (one `?k=v1&k=v2` per item).
        if location == "query" and param_schema.get("type") == "array":
            param_info["style"] = "form"
            param_info["explode"] = True

        if marker is not None and marker.deprecated:
            param_info["deprecated"] = True

        parameters.append(param_info)

    _declare_undocumented_path_params(info, parameters)
    return _RouteParameters(parameters, request_body_schema, form_fields, body_fields, scalar_body)


def _declare_undocumented_path_params(info: Any, parameters: list[dict]) -> None:
    """Document a path parameter no handler parameter declares.

    A route's path parameters are part of its contract whether or not the
    signature names one: a dependency reading `request.path_params` consumes the
    same segment. Leaving it out also makes the document invalid - OpenAPI 3.1
    requires every template expression in the path to have a `path` parameter -
    so a caller reading the schema could not know to supply it at all.
    """
    template = info.path_template
    if not template:
        return
    declared = {p["name"] for p in parameters if p["in"] == "path"}
    for name, schema in path_param_schemas(template).items():
        if name in declared:
            continue
        parameters.append({"name": name, "in": "path", "required": True, "schema": schema})


def _extract_request_body(
    request_body_schema: dict | None,
    form_fields: list[_FormField],
    body_fields: list[_BodyField] | None = None,
    scalar_body: _ScalarBody | None = None,
) -> dict | None:
    """Build the OpenAPI `requestBody` object, or `None` when no body params exist.

    A JSON Pydantic body takes precedence over form fields, matching the
    monolithic implementation. When only form fields are present, the
    media type is `multipart/form-data` if any field is a file upload
    (OpenAPI 3.1 Sec. 4.8.10.4), otherwise `application/x-www-form-urlencoded`.

    A JSON body is always required - the resolver 422s on a missing body. A form
    body whose every field is optional is omittable, so it is documented
    `required: false` to match the runtime.
    """
    if request_body_schema:
        return {
            "required": True,
            "content": {MIME_JSON: {"schema": request_body_schema}},
        }
    # `Body(embed=True)` params are named keys of one JSON object body. The body
    # is only required when at least one of them is, matching a resolver that
    # accepts an absent body when every field carries a default.
    if body_fields:
        embed_properties: dict[str, Any] = {}
        embed_required: list[str] = []
        for body_field in body_fields:
            bname = body_field.alias
            bschema = body_field.schema
            breq = body_field.required
            embed_properties[bname] = bschema
            if breq:
                embed_required.append(bname)
        embed_schema: dict[str, Any] = {"type": "object", "properties": embed_properties}
        if embed_required:
            embed_schema["required"] = embed_required
        return {
            "required": bool(embed_required),
            "content": {MIME_JSON: {"schema": embed_schema}},
        }
    # A non-embedded `Body()` over a non-model receives the whole JSON body, so
    # the body schema is that value's own schema rather than an object wrapper.
    if scalar_body is not None:
        schema, required = scalar_body.schema, scalar_body.required
        return {"required": required, "content": {MIME_JSON: {"schema": schema}}}
    if not form_fields:
        return None
    has_file = any(form_field.is_file for form_field in form_fields)
    media_type = MIME_MULTIPART_FORM_DATA if has_file else MIME_FORM_URLENCODED
    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    for form_field in form_fields:
        properties[form_field.alias] = form_field.schema
        if form_field.required:
            required_fields.append(form_field.alias)
    body_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required_fields:
        body_schema["required"] = required_fields
    return {
        "required": bool(required_fields),
        "content": {media_type: {"schema": body_schema}},
    }


# Component-schema names for the auto-generated validation-error response. The
# `{"detail": [{"loc", "msg", "type"}, ...], "status_code": 422}` payload these
# describe is what the dispatcher emits for a failed path/query/header/cookie/
# body/form parameter, so the document advertises the body a client receives.
# (`request_validation_exception_handler` is exported for applications that want
# to install it; it is not registered by default and is not this path.)
_VALIDATION_ERROR_SCHEMA_NAME = "ValidationError"
_HTTP_VALIDATION_ERROR_SCHEMA_NAME = "HTTPValidationError"

# The auto-added 422 response points at this internal placeholder ref rather than
# the real component name. At finalize the placeholder is resolved to the actual
# (collision-free) envelope component everywhere it appears. Using a placeholder
# means a user-declared 422 that legitimately references a model named
# `HTTPValidationError` is never mistaken for - or rewritten as - the auto entry.
_AUTO_VALIDATION_ERROR_REF = "#/components/schemas/__veloce_auto_http_validation_error__"

# Reference key (string status code) for the auto-added 422 entry.
_VALIDATION_ERROR_STATUS = str(HTTP_422_UNPROCESSABLE_ENTITY)

# Component schemas for the validation-error envelope, mirroring the per-error
# items `RequestValidationError` carries (`loc` / `msg` / `type`). `loc` is a
# mixed string/int path (`["query", "n"]`, `["body", 0, "name"]`), so its items
# accept either; the wrapper nests an array of these under `detail`.
_VALIDATION_ERROR_COMPONENT_SCHEMAS: dict[str, dict[str, Any]] = {
    _VALIDATION_ERROR_SCHEMA_NAME: {
        "type": "object",
        "title": _VALIDATION_ERROR_SCHEMA_NAME,
        "properties": {
            "loc": {
                "type": "array",
                "title": "Location",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "msg": {"type": "string", "title": "Message"},
            "type": {"type": "string", "title": "Error Type"},
        },
        "required": ["loc", "msg", "type"],
    },
    _HTTP_VALIDATION_ERROR_SCHEMA_NAME: {
        "type": "object",
        "title": _HTTP_VALIDATION_ERROR_SCHEMA_NAME,
        "properties": {
            "detail": {
                "type": "array",
                "title": "Detail",
                "items": {"$ref": f"#/components/schemas/{_VALIDATION_ERROR_SCHEMA_NAME}"},
            },
            # The dispatcher emits this alongside `detail`; a schema that omits
            # it describes a body no client actually receives.
            "status_code": {"type": "integer", "title": "Status Code"},
        },
    },
}


def _references_validation_error_schema(operation: dict[str, Any]) -> bool:
    """Return True when `operation`'s 422 entry points at `HTTPValidationError`.

    Used to decide whether the shared validation-error component schemas need to
    be emitted. A user-declared 422 (different `$ref` or inline schema) does not
    trigger registration of the auto component.
    """
    entry = operation.get("responses", {}).get(_VALIDATION_ERROR_STATUS)
    if not isinstance(entry, dict):
        return False
    schema = entry.get("content", {}).get(MIME_JSON, {}).get("schema", {})
    return isinstance(schema, dict) and schema.get("$ref") == _AUTO_VALIDATION_ERROR_REF


def _unique_component_name(base: str, taken: dict[str, Any]) -> str:
    """Return `base`, or `base_2`, `base_3`, ... when `base` is already a key."""
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def _repoint_validation_error_refs(schema: dict[str, Any], http_name: str) -> None:
    """Resolve the auto-422 placeholder `$ref` to the real envelope component.

    The auto-added 422 responses carry `_AUTO_VALIDATION_ERROR_REF`; this rewrites
    only those to the finalized (collision-free) component name. Because the
    placeholder is internal, a user-declared 422 that references a model named
    `HTTPValidationError` never matches and is left untouched.
    """
    new_ref = f"#/components/schemas/{http_name}"
    groups = [schema.get("paths", {})]
    if "webhooks" in schema:
        groups.append(schema["webhooks"])
    for group in groups:
        for operations in group.values():
            if not isinstance(operations, dict):
                continue
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                entry = operation.get("responses", {}).get(_VALIDATION_ERROR_STATUS)
                if not isinstance(entry, dict):
                    continue
                target = entry.get("content", {}).get(MIME_JSON, {}).get("schema")
                if isinstance(target, dict) and target.get("$ref") == _AUTO_VALIDATION_ERROR_REF:
                    target["$ref"] = new_ref


def _extract_responses(
    info: Any, schemas_registry: SchemaRegistry, has_validatable_params: bool
) -> dict[str, dict]:
    """Build the operation `responses` map.

    Seeds with the success response (re-keyed to `info.status_code` when
    not 200), attaches `response_model` under the primary status, then
    merges entries from `info.responses` - each carrying `model`,
    `description`, or any free-form OpenAPI keys.

    When `has_validatable_params` is set and the user has not already declared
    a 422, an `HTTPValidationError` entry is added: the resolver raises
    `RequestValidationError` (rendered as a 422 `{"detail": [...]}`) whenever a
    request-bound parameter fails validation, so the documented responses match
    what the operation actually returns.
    """
    responses: dict[str, dict] = {
        str(HTTP_200_OK): {"description": info.response_description},
    }
    primary_status = str(info.status_code if info.status_code else HTTP_200_OK)
    if primary_status != str(HTTP_200_OK):
        # Re-key the seeded default to the route's chosen status.
        responses[primary_status] = responses.pop(str(HTTP_200_OK))

    if info.response_model is not None:
        resp_schema = _response_model_to_schema(
            info.response_model,
            schemas_registry,
            info.response_model_include,
            info.response_model_exclude,
        )
        if resp_schema is not None:
            responses[primary_status]["content"] = {MIME_JSON: {"schema": resp_schema}}

    # Auto-add the validation-error response for operations whose request is
    # validated. Skipped when the user already declares a 422 - via `responses=`
    # OR `openapi_extra={"responses": {"422": ...}}` (which is deep-merged onto
    # the operation later) - so a custom 422 shape / media type is preserved
    # rather than overwritten. Operations with no validatable parameter never
    # advertise a 422 the resolver cannot raise.
    _openapi_extra_responses = (info.openapi_extra or {}).get("responses") or {}
    if (
        has_validatable_params
        and _VALIDATION_ERROR_STATUS not in responses
        and _VALIDATION_ERROR_STATUS not in {str(s) for s in (info.responses or {})}
        and _VALIDATION_ERROR_STATUS not in {str(s) for s in _openapi_extra_responses}
    ):
        responses[_VALIDATION_ERROR_STATUS] = {
            "description": "Validation Error",
            "content": {MIME_JSON: {"schema": {"$ref": _AUTO_VALIDATION_ERROR_REF}}},
        }

    for status_code, spec in (info.responses or {}).items():
        key = str(status_code)
        existing = responses.setdefault(key, {})
        if not isinstance(spec, dict):
            continue
        extra_model = spec.get("model")
        if extra_model is not None:
            extra_schema = _response_model_to_schema(extra_model, schemas_registry)
            if extra_schema is not None:
                existing.setdefault("content", {})[MIME_JSON] = {"schema": extra_schema}
        if "description" in spec:
            existing["description"] = spec["description"]
        elif "description" not in existing:
            existing["description"] = ""
        # Allow free-form merging of any other keys (headers, links, etc.).
        for k, v in spec.items():
            if k in ("model", "description"):
                continue
            existing[k] = v

    return responses


# ── Security scheme discovery ──────────────────────────────


def _scheme_definition(scheme: Any) -> tuple[str, dict] | None:
    """Return `(name, OpenAPI security scheme object)` for a Security() target.

    The scheme describes itself through `SecurityScheme.openapi_scheme`. A
    nine-branch `isinstance` cascade over the built-in classes lived here and
    returned `None` for everything else, so a user's own `SecurityScheme`
    subclass authenticated correctly at runtime and was published as an
    endpoint with no security requirement - a document asserting the route was
    open. Asking the object means a scheme defined outside this package is
    published like a built-in.

    `None` still means "cannot be described", which the caller reports rather
    than passing off as an unguarded route. The name is the scheme's class name
    so repeated registrations share one `components.securitySchemes` entry.
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

    The descent step both dependency-graph walkers take: introspect, then read
    each parameter's marker however it was spelled. Written twice, a change to
    how a marker is recognised reaches one walker and not the other - and the
    two answer questions (which security schemes guard this route; does
    anything here consume validated input) that must agree about what the graph
    contains. The annotation is the resolved one, falling back to whatever the
    signature carries. Yields nothing for a target that cannot be introspected.
    """
    sig, hints = _handler_intro(target)
    if sig is None:
        return
    for param in sig.parameters.values():
        yield param, _param_marker(param, hints), hints.get(param.name, param.annotation)


def _collect_security_requirements(
    info: Any, registry: dict[str, dict]
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
        # `dep` is either a Depends/Security marker, or a callable that
        # might be one of the known scheme classes.
        if isinstance(dep, Depends):
            target = dep.dependency
            scheme_def = _scheme_definition(target)
            if scheme_def is not None:
                name, definition = scheme_def
                registry.setdefault(name, definition)
                # Scopes only matter for OAuth2; Security() carries them,
                # plain Depends doesn't.
                scopes = list(getattr(dep, "scopes", []) or [])
                requirements.append({name: scopes})
                # Don't recurse past a known scheme - its internals are
                # implementation, not policy.
                return
            if isinstance(target, SecurityScheme):
                # It guards the route but cannot say how. Publishing nothing
                # asserts the route is open, which is the one thing that must
                # not happen quietly - a generated client and the Authorize
                # button both read this as "no credential needed".
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


def _build_operation(
    info: Any,
    method_lower: str,
    schemas_registry: SchemaRegistry,
    security_schemes_registry: dict[str, dict],
) -> dict[str, Any]:
    """Assemble one OpenAPI operation object for a single route entry."""
    # OpenAPI 3.1 Sec. 4.8.10 - operationId must be unique across the document.
    # Explicit override wins; default = `<name>_<method>`. Collisions among the
    # auto-generated form are resolved later in `_disambiguate_operation_ids`.
    op_id = info.operation_id if info.operation_id else f"{info.name}_{method_lower}"
    operation: dict[str, Any] = {
        "summary": info.summary or info.name,
        "operationId": op_id,
        "responses": {"200": {"description": info.response_description}},
    }

    if info.description:
        operation["description"] = info.description
    if info.tags:
        operation["tags"] = info.tags
    if info.deprecated:
        operation["deprecated"] = True
    # OpenAPI 3.1 Sec. 4.8.8 - route-level `callbacks` map emitted verbatim.
    if info.callbacks:
        operation["callbacks"] = info.callbacks

    inputs = _extract_parameters(info, schemas_registry)
    if inputs.parameters:
        operation["parameters"] = inputs.parameters

    request_body = _extract_request_body(
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    if request_body is not None:
        operation["requestBody"] = request_body

    # Walk this route's Security() chain to register OpenAPI security
    # schemes and attach the operation-level `security` requirement.
    security_requirements = _collect_security_requirements(info, security_schemes_registry)
    if security_requirements:
        operation["security"] = security_requirements

    # An operation can return a 422 only when it carries something the resolver
    # validates: a path/query/header/cookie parameter, a JSON body, a form field,
    # or any validated input inside a `Depends(...)` sub-dependency. A handler
    # with none of these never raises `RequestValidationError`, so it must not
    # advertise a 422.
    has_validatable_params = (
        bool(inputs.parameters)
        or request_body is not None
        or _dependency_graph_has_validatable(info)
    )
    operation["responses"] = _extract_responses(info, schemas_registry, has_validatable_params)

    # `openapi_extra` - deep-merge the user-supplied dict over the
    # generated operation. Nested dicts merge key-by-key; scalars and
    # lists in `openapi_extra` overwrite.
    extra = info.openapi_extra
    if extra:
        _deep_merge(operation, extra)

    return operation


def _webhook_request_body(handler: Any, registry: SchemaRegistry) -> dict | None:
    """Return the OpenAPI schema for a webhook handler's Pydantic body param.

    A webhook handler documents the payload an external caller will
    POST; the first BaseModel-typed parameter is treated as that body.
    """
    sig, hints = _handler_intro(handler)
    if sig is None:
        return None
    for pname in sig.parameters:
        if pname in ("self", "request"):
            continue
        annotation = hints.get(pname)
        if annotation and _is_model_type(annotation):
            return registry.ref(annotation, mode="validation")
    return None


def _walk_webhooks(
    app: Any,
    schemas_registry: SchemaRegistry,
    auto_ops: list[tuple[dict[str, Any], str, str]],
) -> dict[str, Any]:
    """Return the OpenAPI 3.1 `webhooks` map from `app.webhooks`.

    Empty when no webhooks router exists or it has no routes. Each entry
    is keyed by event name (the path on `@app.webhooks.post`) and carries
    one operation per HTTP method registered. Each webhook's auto-generated
    operationId is appended to `auto_ops` so it flows through the same
    document-wide disambiguation pass as normal routes; two webhooks sharing a
    handler name (or a webhook colliding with a route) would otherwise emit a
    duplicate operationId, which is invalid for code generation (OpenAPI 3.1
    Sec. 4.8.10).
    """
    webhook_items: dict[str, Any] = {}
    webhooks_router = app.webhooks
    walker = getattr(webhooks_router, "_walk_routes", None) if webhooks_router else None
    if walker is None:
        return webhook_items
    for wpath, wmethods, winfo in walker():
        event = wpath.strip("/") or wpath
        for wmethod in wmethods:
            op: dict[str, Any] = {
                "summary": winfo.summary or winfo.name,
                "operationId": f"{winfo.name}_{wmethod.lower()}",
                "responses": {"200": {"description": winfo.response_description}},
            }
            # The disambiguator keys collisions on a deterministic identifier; a
            # webhook has no URL path, so its event name stands in as the
            # suffix source if a collision must be resolved.
            auto_ops.append((op, event, wmethod.lower()))
            if winfo.description:
                op["description"] = winfo.description
            body = _webhook_request_body(winfo.handler, schemas_registry)
            if body is not None:
                op["requestBody"] = {
                    "required": True,
                    "content": {MIME_JSON: {"schema": body}},
                }
            webhook_items.setdefault(event, {})[wmethod.lower()] = op
    return webhook_items


def _disambiguate_operation_ids(
    auto_ops: list[tuple[dict[str, Any], str, str]],
    explicit_ops: list[tuple[dict[str, Any], str, str]] | None = None,
) -> None:
    """Make every auto-generated operationId unique in place.

    OpenAPI 3.1 Sec. 4.8.10 requires `operationId` unique across the document.
    Two handlers sharing a function name on different paths would otherwise emit
    the same id and break client code generation. Each duplicate after the
    first keeps a deterministic path-derived suffix; a single aggregated WARNING
    lists every collision and its resolution.

    User-pinned operationIds (`explicit_ops`) are reserved up front and never
    rewritten: an auto id clashing with a pinned id (or with another auto id) is
    suffixed instead. Two identical *explicit* ids are a user error the document
    cannot silently fix by renaming a pinned id, so they are surfaced via a
    WARNING rather than left to ship duplicated.
    """
    explicit_ops = explicit_ops or []

    # Reserve every explicit id first so auto ids are disambiguated against them.
    # `assigned` accumulates ids already taken across the whole document, so the
    # suffix search below never collides with a pinned id or an earlier auto id.
    assigned: set[str] = set()
    explicit_seen: dict[str, tuple[str, str]] = {}
    explicit_dupes: list[str] = []
    for op, path, method in explicit_ops:
        op_id = op["operationId"]
        if op_id in explicit_seen:
            first_path, first_method = explicit_seen[op_id]
            explicit_dupes.append(
                f"{op_id} ({first_method.upper()} {first_path} and {method.upper()} {path})"
            )
        else:
            explicit_seen[op_id] = (path, method)
            assigned.add(op_id)

    by_id: dict[str, list[tuple[dict[str, Any], str, str]]] = {}
    for op, path, method in auto_ops:
        by_id.setdefault(op["operationId"], []).append((op, path, method))

    resolutions: list[str] = []
    for op_id, group in by_id.items():
        # An auto id is left on its bare form only when nothing else (no pinned
        # id, no earlier auto group) already claimed it. Otherwise every member
        # of the group, including the first, is suffixed from its path so the
        # assignment is stable across regenerations of the document.
        free_first = op_id not in assigned
        for index, (op, path, method) in enumerate(group):
            if index == 0 and free_first:
                assigned.add(op_id)
                continue
            suffix = "_".join(seg for seg in path.split("/") if seg) or "root"
            candidate = f"{op_id}__{suffix}"
            tail = 1
            while candidate in assigned:
                candidate = f"{op_id}__{suffix}_{tail}"
                tail += 1
            assigned.add(candidate)
            op["operationId"] = candidate
            resolutions.append(f"{op_id} -> {candidate} ({method.upper()} {path})")

    if resolutions:
        _logger.warning(
            "Duplicate OpenAPI operationId(s) auto-disambiguated; pin "
            "`operation_id=` on the affected routes to silence this. "
            "Resolutions: %s",
            "; ".join(resolutions),
        )

    if explicit_dupes:
        _logger.warning(
            "Duplicate explicit OpenAPI operationId(s) - these violate OpenAPI "
            "3.1 Sec. 4.8.10 and cannot be auto-resolved without overriding a "
            "pinned `operation_id=`; rename one of each pair: %s",
            "; ".join(explicit_dupes),
        )


def _validate_document(schema: dict[str, Any]) -> None:
    """Lightweight structural pass over the assembled OpenAPI document.

    Opt-in (gated on `app.validate_openapi`, defaulting to `app.debug`) so the
    common production build pays nothing. Catches malformed entries from the
    free-form `info.responses` merge, a bad `openapi_extra` `_deep_merge`, or a
    dangling `$ref` before the document reaches Swagger UI, raising a
    `ValueError` that names the offending path and method.
    """
    # `info.title` and `info.version` are REQUIRED and are strings - OpenAPI 3.1
    # Sec. 4.8.2. The pass checked `$ref`s and container shapes and never these,
    # so `validate_openapi=True` accepted a document it was asked to reject.
    info = schema.get("info")
    if not isinstance(info, dict):
        raise ValueError("OpenAPI document `info` must be an object")
    for field in ("title", "version"):
        value = info.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"OpenAPI document `info.{field}` must be a non-empty string, got {value!r}"
            )

    schemas = schema.get("components", {}).get("schemas", {})
    paths = schema.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document `paths` must be an object")

    known_refs = {f"#/components/schemas/{name}" for name in schemas}

    def check_refs(node: Any, where: str) -> None:
        for mapping in _iter_dicts(node):
            ref = mapping.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith("#/components/schemas/")
                and ref not in known_refs
            ):
                raise ValueError(f"{where}: unresolved schema $ref {ref!r}")

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            raise ValueError(f"path {path!r}: operations container must be an object")
        for method, operation in methods.items():
            where = f"{method.upper()} {path}"
            if not isinstance(operation, dict):
                raise ValueError(f"{where}: operation must be an object")
            responses = operation.get("responses")
            if not isinstance(responses, dict) or not responses:
                raise ValueError(f"{where}: operation must declare at least one response")
            for param in operation.get("parameters", []) or []:
                if not isinstance(param, dict) or "name" not in param or "in" not in param:
                    raise ValueError(f"{where}: every parameter needs `name` and `in`")
            check_refs(operation, where)

    check_refs(schemas, "components.schemas")


# ── Public API ─────────────────────────────────────────────


def get_openapi_schema(app: Any) -> dict[str, Any]:
    """Generate OpenAPI 3.1 schema from the app's registered routes."""
    schema: dict[str, Any] = {
        # `app.openapi_version` is documented as "the spec version string emitted
        # in the document", and was emitted nowhere: setting it changed the
        # attribute and not the document.
        "openapi": app.openapi_version,
        "info": _build_info_object(app),
        "paths": {},
        "components": {"schemas": {}},
    }

    if app.servers:
        schema["servers"] = app.servers
    if app.openapi_tags:
        schema["tags"] = app.openapi_tags
    # OpenAPI 3.1 Sec. 4.8.11 - top-level `externalDocs` object.
    if app.openapi_external_docs:
        schema["externalDocs"] = app.openapi_external_docs

    schemas_registry = SchemaRegistry(separate_input_output=app.separate_input_output_schemas)
    security_schemes_registry: dict[str, dict] = {}

    # Operations whose operationId was auto-generated (no explicit override),
    # recorded so a deterministic suffix can resolve duplicates afterwards.
    auto_ops: list[tuple[dict[str, Any], str, str]] = []
    # Operations carrying a user-pinned `operation_id`. These are reserved
    # during disambiguation so an auto id colliding with a pinned id suffixes
    # the AUTO one (a user's explicit id is never rewritten), and two identical
    # pinned ids are detected and warned about (a user error the document can't
    # silently fix by renaming).
    explicit_ops: list[tuple[dict[str, Any], str, str]] = []

    # Set once any operation auto-adds the validation-error response, so the
    # `HTTPValidationError` / `ValidationError` component schemas are emitted only
    # when something actually references them.
    needs_validation_error_schema = False

    for method, path, info in app.iter_routes():
        # OpenAPI 3.1's Path Item Object has no `query` field, so a QUERY route
        # cannot be represented without emitting an invalid operation. Omit it
        # rather than produce an invalid document (native support awaits the
        # OpenAPI 3.2 `query` operation - RFC 10008).
        if method == HTTP_METHOD_QUERY:
            continue
        method_lower = method.lower()
        if path not in schema["paths"]:
            schema["paths"][path] = {}
        operation = _build_operation(
            info, method_lower, schemas_registry, security_schemes_registry
        )
        if _references_validation_error_schema(operation):
            needs_validation_error_schema = True
        schema["paths"][path][method_lower] = operation
        if info.operation_id:
            explicit_ops.append((operation, path, method_lower))
        else:
            auto_ops.append((operation, path, method_lower))

    # Webhook operations are appended to `auto_ops` so the disambiguation pass
    # below dedupes operationIds across BOTH routes and webhooks deterministically
    # (routes first in collection order, then webhooks in walker order).
    webhook_items = _walk_webhooks(app, schemas_registry, auto_ops)
    if webhook_items:
        schema["webhooks"] = webhook_items

    if app.disambiguate_operation_ids:
        _disambiguate_operation_ids(auto_ops, explicit_ops)

    components_schemas = schemas_registry.finalize(schema)
    # Add the validation-error component schemas only when an operation referenced
    # them. If a user model already occupies `HTTPValidationError` / `ValidationError`,
    # register the auto envelope under collision-free names and repoint the 422
    # `$ref`s, so the documented validation-error payload is never silently bound to
    # an unrelated user model.
    if needs_validation_error_schema:
        val_name = _unique_component_name(_VALIDATION_ERROR_SCHEMA_NAME, components_schemas)
        http_name = _unique_component_name(_HTTP_VALIDATION_ERROR_SCHEMA_NAME, components_schemas)
        val_body = copy.deepcopy(_VALIDATION_ERROR_COMPONENT_SCHEMAS[_VALIDATION_ERROR_SCHEMA_NAME])
        val_body["title"] = val_name
        http_body = copy.deepcopy(
            _VALIDATION_ERROR_COMPONENT_SCHEMAS[_HTTP_VALIDATION_ERROR_SCHEMA_NAME]
        )
        http_body["title"] = http_name
        http_body["properties"]["detail"]["items"]["$ref"] = f"#/components/schemas/{val_name}"
        components_schemas[val_name] = val_body
        components_schemas[http_name] = http_body
        # Operations reference the auto-422 via an internal placeholder; resolve
        # it to the finalized component name everywhere (always, since the
        # placeholder is never a real component).
        _repoint_validation_error_refs(schema, http_name)
    if components_schemas:
        schema["components"]["schemas"] = components_schemas
    if security_schemes_registry:
        schema["components"]["securitySchemes"] = security_schemes_registry

    validate = app.validate_openapi
    if validate is None:
        validate = bool(app.debug)
    if validate:
        _validate_document(schema)

    return schema
