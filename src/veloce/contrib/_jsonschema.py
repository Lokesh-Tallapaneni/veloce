"""Python type — JSON Schema, and the `$defs` registry that names the results.

The lower of the two layers `contrib/openapi.py` used to hold. It converts a
Python annotation into a JSON Schema fragment and gives every model schema a
stable component name; it knows nothing about OpenAPI documents, operations or
routes.

Separate because it has a second consumer: `contrib.mcp.plan_bridge` builds MCP
tool input schemas from the same conversion, and used to reach four private
names out of the document generator to get it — so a signature change here broke
`contrib.mcp` silently, and importing `veloce.contrib.mcp` eagerly loaded the
whole OpenAPI assembler to obtain a tool schema.
"""

from __future__ import annotations

import collections.abc
import copy
import datetime
import enum
import hashlib
import inspect
import logging
import pathlib
import types
import uuid
import weakref
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, create_model

from veloce._internal import _SCALAR_JSON_SCHEMAS
from veloce._model_backend import (
    _msgspec,
    adapter_for,
    is_adaptable_model,
    is_msgspec_struct,
    is_pydantic_model,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

_logger = logging.getLogger(__name__)


def _is_model_type(annotation: Any) -> bool:
    """Return True for a Pydantic ``BaseModel`` or a ``msgspec.Struct`` annotation.

    The single gate every request-body / response / list-item schema site uses,
    so both backends register a component schema and resolve to a ``$ref`` the
    same way.
    """
    if is_pydantic_model(annotation):
        return True
    return is_msgspec_struct(annotation)


def _literal_enum_schema(values: list[Any]) -> dict[str, Any]:
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
    int: _SCALAR_JSON_SCHEMAS["integer"],
    float: _SCALAR_JSON_SCHEMAS["number"],
    bool: {"type": "boolean"},
    bytes: {"type": "string", "format": "byte"},
    list: {"type": "array", "items": {}},
    dict: {"type": "object"},
    datetime.datetime: _SCALAR_JSON_SCHEMAS["date-time"],
    datetime.date: _SCALAR_JSON_SCHEMAS["date"],
    datetime.time: _SCALAR_JSON_SCHEMAS["time"],
    datetime.timedelta: {"type": "string", "format": "duration"},
    uuid.UUID: _SCALAR_JSON_SCHEMAS["uuid"],
    Decimal: _SCALAR_JSON_SCHEMAS["number"],
    # `PurePath` covers the platform-specific subclasses too.
    pathlib.PurePath: _SCALAR_JSON_SCHEMAS["path"],
    pathlib.Path: _SCALAR_JSON_SCHEMAS["path"],
}


def _python_type_to_schema(annotation: Any) -> dict[str, Any]:
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

    def finalize(self, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Resolve placeholders, rewrite `document` in place, return schemas.

        Assigns each entry a final component name (bare class name when that
        name is unique across the document, otherwise qualified by the module
        tail), folds a byte-identical serialization variant onto its validation
        twin, then rewrites every placeholder `$ref` reachable from `document`.
        """
        token_to_name: dict[str, str] = {}
        components: dict[str, dict[str, Any]] = {}

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
            # The nested-def half below checks for a committed component of the
            # same name and different content; this write did not, so a model
            # reachable two ways - nested inside one route's request body, and as
            # another route's response model - had whichever came second silently
            # replace the first. With a `computed_field` on it, the published
            # request schema then required a read-only field the model does not
            # accept as input. The first writer stands, matching
            # `_diverging_def_renames`, and `token_to_name` is updated before the
            # `$ref` rewrite below so every reference follows the new name.
            committed = components.get(name)
            if committed is not None and committed != entry.body:
                name = _unique_component_name(
                    f"{name}-Output" if entry.mode == "serialization" else name, components
                )
                token_to_name[entry.token] = name
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
        self, defs: dict[str, dict[str, Any]], components: dict[str, dict[str, Any]]
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
    def _unique_def_name(components: dict[str, dict[str, Any]], base: str) -> str:
        """Return a component name derived from `base` not already in use.

        The `-Output` suffix mirrors the top-level serialization variant naming
        so a diverging nested model reads as its owner's output twin; finding a
        free name from there is `_unique_component_name`'s job, which this used
        to restate with the arguments in the other order.
        """
        return _unique_component_name(f"{base}-Output", components)

    def _all_tokens(self) -> list[str]:
        return [self._entries[k].token for k in self._order]


def _iter_dicts(node: Any) -> collections.abc.Iterator[dict[str, Any]]:
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
        self.defs: dict[str, dict[str, Any]] = {}
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


def _copy_with_local_defs(node: dict[str, Any], renames: dict[str, str]) -> dict[str, Any]:
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


def _register_schema(
    name: str, schema: dict[str, Any], registry: dict[str, dict[str, Any]]
) -> None:
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


def _adapted_to_schema(model: Any, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
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
    registry: dict[str, dict[str, Any]],
    mode: str = "validation",
    by_alias: bool = True,
) -> dict[str, Any]:
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
#: `response_model_exclude`, so two routes filtering a model the same way share
#: one component rather than emitting two.
#:
#: Keyed by the model itself, held weakly, with the filter as the inner key. An
#: earlier version keyed on `id(model)` and held no reference: CPython recycles
#: the address of a collected model, so a model built afterwards could land on
#: the same id and be served the earlier model's derived class - a route
#: documented as returning fields it has never heard of. The weak key removes
#: that and the unbounded growth together, since the entry goes when the model
#: does.
_FilterKey = tuple[frozenset[str] | None, frozenset[str] | None]
_FILTERED_MODELS: weakref.WeakKeyDictionary[Any, dict[_FilterKey, Any]] = (
    weakref.WeakKeyDictionary()
)


#: Longest joined field list to spell out in a derived component's name.
_FILTERED_NAME_LIMIT = 48


def _filtered_struct(
    model: Any, include: set[str] | None, exclude: set[str] | None, key: _FilterKey
) -> Any:
    """Derive the `msgspec.Struct` half of `_filtered_response_model`.

    Separate because the two backends describe their fields differently -
    `model_fields` against `__struct_fields__` and `__annotations__` - and
    because a Struct is derived with `msgspec.defstruct` rather than
    `create_model`. The naming rule is shared, so a filtered component key reads
    the same whichever backend declared the model.
    """
    annotations = _collect_annotations(model)
    surviving = [
        name
        for name in model.__struct_fields__
        if (include is None or name in include) and not (exclude and name in exclude)
    ]
    if not surviving:
        # As above: an empty object is a less useful document than the model.
        return model

    suffix = "_".join(sorted(surviving))
    if len(suffix) > _FILTERED_NAME_LIMIT:
        suffix = hashlib.blake2b(suffix.encode(), digest_size=6).hexdigest()
    derived = _msgspec.defstruct(
        f"{model.__name__}_{suffix}",
        [(name, annotations.get(name, Any)) for name in surviving],
    )
    _FILTERED_MODELS.setdefault(model, {})[key] = derived
    return derived


def _collect_annotations(model: Any) -> dict[str, Any]:
    """Every annotation on `model` and its bases, nearest definition winning."""
    collected: dict[str, Any] = {}
    for base in reversed(model.__mro__):
        collected.update(getattr(base, "__annotations__", {}))
    return collected


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
    key: _FilterKey = (
        frozenset(include) if include else None,
        frozenset(exclude) if exclude else None,
    )
    derived_for_model = _FILTERED_MODELS.get(model)
    if derived_for_model is not None:
        cached = derived_for_model.get(key)
        if cached is not None:
            return cached
    # `_is_model_type` - the gate that reaches here - admits a `msgspec.Struct`
    # as well as a Pydantic model, and only the latter has `model_fields`. A
    # single Struct route carrying a filter raised `AttributeError` here and
    # took the whole document with it, because `app.openapi()` builds one
    # document for every route.
    if not is_pydantic_model(model):
        return _filtered_struct(model, include, exclude, key)

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
    _FILTERED_MODELS.setdefault(model, {})[key] = derived
    return derived


def _response_model_to_schema(
    response_model: Any,
    registry: SchemaRegistry,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
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
        variants: list[dict[str, Any]] = []
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


def _unique_component_name(base: str, taken: dict[str, Any]) -> str:
    """Return `base`, or `base_2`, `base_3`, ... when `base` is already a key."""
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"
