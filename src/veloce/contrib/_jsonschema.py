"""Python type — JSON Schema, and the `$defs` registry that names the results.

The lower of the two layers `contrib/openapi.py` holds. It converts a Python
annotation into a JSON Schema fragment and gives every model schema a stable
component name; it knows nothing about OpenAPI documents, operations or routes.

Separate because it has a second consumer: `contrib.mcp.plan_bridge` builds MCP
tool input schemas from the same conversion, so neither door reaches into the
other's document generator for one.
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

from veloce._handler_plan import _fixed_shape_args
from veloce._internal import _SCALAR_JSON_SCHEMAS
from veloce._model_backend import (
    ModelBackend,
    _msgspec,
    adapter_for,
    backend_of,
    is_adaptable_model,
    is_msgspec_struct,
    is_pydantic_model,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

_logger = logging.getLogger(__name__)


def _is_model_type(annotation: Any) -> bool:
    """Return True for an annotation that gets its own component schema.

    The single gate every request-body / response / list-item schema site uses,
    asked of `backend_of` so this gate and the route contract's cannot disagree.
    """
    return backend_of(annotation) is not ModelBackend.NONE


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


# A hit returns a shallow copy, whose shared `items` dict is never written to.
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

    This builds the schema for a non-body parameter (query / path / header /
    cookie) or a form field. Those values arrive as raw strings, so the schema
    documents only what that pipeline can deliver: a Pydantic model keeps
    `{"type": "string"}`, since the resolver parses the raw string as JSON.

    Scalars keep their richer `format` / `enum` keywords, and a nullable scalar
    unwraps to the inner schema. A multi-member union is schema'd by which
    branches a *string* input can reach under Pydantic's smart coercion: one
    including `str` collapses to `{"type": "string"}`, while one with no
    string-accepting member emits an `anyOf`.
    """
    if annotation is None or annotation is inspect.Parameter.empty:
        return {"type": "string"}
    # `Any` is an empty schema by JSON Schema convention, not a string default.
    if annotation is Any:
        return {}

    origin = get_origin(annotation)
    # Only a *bare* model annotation is JSON-decoded, so a union member is not.
    if origin is Union or origin is types.UnionType:
        members = get_args(annotation)
        inner = [a for a in members if a is not type(None)]
        if len(inner) == 1:
            return _python_type_to_schema(inner[0])
        if any(m is str or m is bytes for m in inner):
            return {"type": "string"}
        reachable = [m for m in inner if not _is_model_type(m)]
        if not reachable:
            # Every member is a model: a bare object, not a string it rejects.
            return {"type": "object"}
        if len(reachable) == 1:
            return _python_type_to_schema(reachable[0])
        return {"anyOf": [_python_type_to_schema(m) for m in reachable]}

    # `list[Tag]` 422s on a JSON-array string, so the item schema is a string.
    if origin in _SEQUENCE_ORIGINS:
        args = get_args(annotation)
        positional = _fixed_shape_args(annotation)
        if positional is not None:
            # A type per position and a length: JSON Schema 2020-12 `prefixItems`.
            fixed: dict[str, Any] = {
                "type": "array",
                "minItems": len(positional),
                "maxItems": len(positional),
            }
            if positional:
                # `prefixItems` needs one schema, so `tuple[()]` uses its length.
                fixed["prefixItems"] = [_python_type_to_schema(arg) for arg in positional]
            return fixed
        item = _python_type_to_schema(args[0]) if args else {}
        schema: dict[str, Any] = {"type": "array", "items": item}
        if origin is set or origin is frozenset:
            # The resolver drops duplicates, so the document has to say it does.
            schema["uniqueItems"] = True
        return schema
    # A typed `additionalProperties` would advertise a shape always rejected.
    if origin is dict:
        return {"type": "object"}
    if origin is Literal:
        return _literal_enum_schema(list(get_args(annotation)))

    mapped = _SCALAR_TYPE_SCHEMAS.get(annotation)
    if mapped is not None:
        return dict(mapped)

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _literal_enum_schema([member.value for member in annotation])

    if is_pydantic_model(annotation):
        return {"type": "string"}

    return {"type": "string"}


# Written until `SchemaRegistry.finalize` assigns the real component names.
_REF_PLACEHOLDER_PREFIX = "#/$veloce-schema/"


# Pydantic emits nested-model references under this prefix.
_PYDANTIC_DEF_PREFIX = "#/$defs/"


class SchemaRegistry:
    """Identity-keyed registry for the component schemas of one document.

    Models are keyed on the class object plus the JSON-Schema mode, so two
    classes sharing a ``__name__`` never overwrite each other and a model whose
    input and output shapes diverge can publish both. References handed back
    during assembly are placeholders that `finalize` resolves to readable
    component names.
    """

    __slots__ = ("_entries", "_order", "separate_input_output")

    def __init__(self, separate_input_output: bool = True) -> None:
        self._entries: dict[tuple[int, str], _SchemaEntry] = {}
        self._order: list[tuple[int, str]] = []
        self.separate_input_output = separate_input_output

    def ref(self, model: type[BaseModel], mode: str = "validation") -> dict[str, str]:
        """Register `model` under `mode` and return a placeholder `$ref`."""
        # Opting out of split schemas keeps one name per model.
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

        A bare class name when it is unique across the document, otherwise
        qualified by the module tail; a byte-identical serialization variant
        folds onto its validation twin.
        """
        token_to_name: dict[str, str] = {}
        components: dict[str, dict[str, Any]] = {}

        # `-Output` only on divergence, so the common case keeps one name.
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
                    # The FULL schema: a nested def can diverge alone.
                    if twin.body == entry.body and twin.defs == entry.defs:
                        entry.alias_token = twin.token
                        continue
                    # The request keeps the bare name, the response `-Output`.
                    base = f"{base}-Output"
            wanted.setdefault(base, []).append(entry)

        for base, group in wanted.items():
            if len(group) == 1:
                token_to_name[group[0].token] = base
            else:
                # Qualified by the module tail, then by a numeric one.
                used: dict[str, int] = {}
                for entry in group:
                    seg = (entry.model.__module__ or "").rsplit(".", 1)[-1]
                    candidate = f"{base}__{seg}" if seg else base
                    seen = used.get(candidate, 0)
                    used[candidate] = seen + 1
                    if seen:
                        candidate = f"{candidate}_{seen}"
                    token_to_name[entry.token] = candidate

        # A later owner's diverging def gets its own `-Output` variant.
        for key in self._order:
            entry = self._entries[key]
            if entry.alias_token is not None:
                continue
            name = token_to_name[entry.token]
            # The second write of a model reachable two ways must not replace it.
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
                    # Under the unique name, refs repointed at renamed children.
                    rewritten = _copy_with_local_defs(def_schema, local_renames)
                    components[target] = rewritten
                elif def_name not in components:
                    components[def_name] = def_schema
            if local_renames:
                _rewrite_local_defs(entry.body, local_renames)

        # Built once, so the rewrite is linear rather than O(n^2) per token.
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
        the same name, OR when it transitively references one that diverges - so
        an identical wrapper over a diverging child still gets its own variant.
        Computed as a fixpoint over the `#/$defs/...` reference graph.
        """
        diverging: set[str] = set()
        for def_name, def_schema in defs.items():
            existing = components.get(def_name)
            if existing is not None and existing != def_schema:
                diverging.add(def_name)

        # Until stable: any def that reaches a diverging def is itself diverging.
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

        The `-Output` suffix mirrors the top-level serialization variant naming.
        """
        return _unique_component_name(f"{base}-Output", components)

    def _all_tokens(self) -> list[str]:
        return [self._entries[k].token for k in self._order]


def _iter_dicts(node: Any) -> collections.abc.Iterator[dict[str, Any]]:
    """Yield every mapping in a JSON tree, the outermost first.

    Iterative rather than recursive, so a model referring to itself through a
    long chain is bounded by memory rather than the recursion limit. Callers may
    reassign values on a yielded mapping, but must not add or remove keys while
    iterating.
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
    is logged at WARNING and `/docs` still renders.
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

    Veloce's JSON encoder base64-encodes `bytes`, so a bytes field travels as a
    base64 string - RFC 4648 `byte` - not the raw `binary` Pydantic emits.
    """
    for mapping in _iter_dicts(node):
        if mapping.get("type") == "string" and mapping.get("format") == "binary":
            mapping["format"] = "byte"


class _SchemaEntry:
    """One `(model, mode)` registration awaiting name assignment."""

    __slots__ = ("alias_token", "body", "defs", "mode", "model", "token")

    # Monotonic, so placeholder strings stay short and process-unique.
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
                # A dataclass is described by the adapter that validates it.
                schema = adapter_for(self.model).json_schema()
            else:
                schema = self.model.model_json_schema(mode=self.mode)  # type: ignore[arg-type]
        except Exception as exc:
            _warn_schema_fallback(f"{self.model.__name__} ({self.mode} mode)", exc)
            self.body = {"type": "object"}
            return
        _rewrite_byte_format(schema)
        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                self.defs[def_name] = def_schema
            del schema["$defs"]
        self.body = schema

    def _build_msgspec(self) -> None:
        """Populate `body` / `defs` from a `msgspec.Struct`'s JSON Schema.

        msgspec has no separate validation / serialization shape, so `mode` is not
        consulted. The `#/$defs/{name}` ref template matches the prefix the
        document rewriter repoints, so nested structs need no translation.
        """
        try:
            schemas, components = _msgspec.json.schema_components(
                [self.model], ref_template="#/$defs/{name}"
            )
        except Exception as exc:
            _warn_schema_fallback(f"{self.model.__name__} (msgspec)", exc)
            self.body = {"type": "object"}
            return
        # A struct with no nested refs comes back inline rather than as a `$ref`.
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

    The source schema is shared with the validation owner, so it is copied
    before its internal references are redirected at the renamed children.
    """
    copied = copy.deepcopy(node)
    _rewrite_local_defs(copied, renames)
    return copied


def _rewrite_local_defs(node: Any, renames: dict[str, str]) -> None:
    """Repoint `#/$defs/<old>` refs in `node` to `#/components/schemas/<new>`.

    Only the listed `$defs` names are rewritten, leaving every other ref for the
    global pass.
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
    # A recursive model's bare self-`$ref` would clobber the real definition.
    if not (list(schema.keys()) == ["$ref"] and name in registry):
        registry[name] = schema


def _adapted_to_schema(model: Any, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert a dataclass / `TypedDict` to a name-keyed `$ref`, extending `registry`.

    Both this and `_pydantic_to_schema` hoist their `$defs` through
    `_register_schema`, so a nested shape resolves the same way either way.
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

    Standalone renderer for callers that build a self-contained schema envelope.
    `mode` selects the JSON Schema variant, ``"validation"`` for request inputs
    or ``"serialization"`` for outputs, and `by_alias` matches the property keys
    to how the value is dumped. The document-wide OpenAPI generator uses
    `SchemaRegistry` instead, which keys on class identity.
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


#: Origins that serialise to a JSON array.
_SEQUENCE_ORIGINS = (list, collections.abc.Sequence, tuple, set, frozenset, collections.abc.Set)


#: Keyed weakly by the model - `id(model)` recycles - with the filter inside.
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

    Separate because the two backends describe their fields differently and a
    Struct is derived with `msgspec.defstruct`; the naming rule is shared.
    """
    annotations = _collect_annotations(model)
    surviving = [
        name
        for name in model.__struct_fields__
        if (include is None or name in include) and not (exclude and name in exclude)
    ]
    if not surviving:
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
    *sends*, so the document must apply them too. Deriving a model rather than
    editing a schema dict keeps naming, `$defs` hoisting and serialization mode
    on the path the unfiltered model already takes.
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
    # `_is_model_type` admits a Struct too, and only a model has `model_fields`.
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
        # An empty object is a less useful document than the model itself.
        return model
    # The name reaches the components section, so it is stable per filter.
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
    - `MyModel` -> `{"$ref": ".../MyModel"}`.
    - `list[MyModel]` (or any `Sequence[MyModel]`) -> array-of-refs.
    - `A | B` / `A | None` -> `oneOf`, `None` as the JSON Schema null type.
    - Anything else -> `None` (caller omits the schema).

    Rendered under the model's serialization JSON Schema, so computed fields are
    documented as clients actually receive them.
    """
    origin = get_origin(response_model)
    if origin in _SEQUENCE_ORIGINS:
        args = get_args(response_model)
        # `tuple[Item, ...]` carries an Ellipsis in its second slot.
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
                # One member with no schema form makes the union inexpressible.
                return None
        if not variants:
            return None
        return variants[0] if len(variants) == 1 else {"oneOf": variants}

    if _is_model_type(response_model):
        filtered = _filtered_response_model(response_model, include, exclude)
        return registry.ref(filtered, mode="serialization")

    return None


#: Types a default can take and still be written into a JSON document.
_JSON_PUBLISHABLE = (str, int, float, bool, type(None), list, dict)

_NO_DEFAULT = object()


def _publishable_default(value: Any, *, publish_null: bool) -> Any:
    """Return the default to publish for a parameter, or `_NO_DEFAULT`.

    Shared by both lowerings: a `default_factory` has no single value to publish
    and a value with no JSON form cannot go in a document. A marker declaring a
    factory has `...` for its default, which is already the answer.

    OpenAPI 3.1 asks that a default validate against its own schema, and the two
    doors emit different schemas for a nullable parameter, so `publish_null` is
    stated by each caller rather than guessed.
    """
    if value is ...:
        return _NO_DEFAULT
    if value is None and not publish_null:
        return _NO_DEFAULT
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, _JSON_PUBLISHABLE):
        return value
    # Anything else has no JSON form the document can carry.
    return _NO_DEFAULT


def _apply_marker_constraints(param_schema: dict[str, Any], marker: Any) -> None:
    """Copy validation / metadata keywords from a `ParamBase` marker onto `param_schema`."""
    if getattr(marker, "title", None):
        param_schema["title"] = marker.title
    if marker.description:
        param_schema["description"] = marker.description
    # An array's length is `minItems` / `maxItems`; the rest go on `items`.
    array = param_schema.get("type") == "array"
    # Collected first, since where they belong depends on the shape.
    scalar: dict[str, Any] = {} if array else param_schema

    if marker.ge is not None:
        scalar["minimum"] = marker.ge
    if marker.le is not None:
        scalar["maximum"] = marker.le
    # JSON Schema 2020-12: gt/lt are the numeric `exclusiveMinimum` / `Maximum`.
    if marker.gt is not None:
        scalar["exclusiveMinimum"] = marker.gt
    if marker.lt is not None:
        scalar["exclusiveMaximum"] = marker.lt
    if marker.min_length is not None:
        param_schema["minItems" if array else "minLength"] = marker.min_length
    if marker.max_length is not None:
        param_schema["maxItems" if array else "maxLength"] = marker.max_length
    if getattr(marker, "multiple_of", None) is not None:
        scalar["multipleOf"] = marker.multiple_of
    if marker.regex is not None:
        scalar["pattern"] = marker.regex
    if array and scalar:
        prefix = param_schema.get("prefixItems")
        if prefix is not None:
            # Every position carries them, matching the resolver's check.
            param_schema["prefixItems"] = [{**dict(entry), **scalar} for entry in prefix]
        else:
            # A shared `items` dict: replace it, never write through it.
            items = param_schema.get("items")
            base = dict(items) if isinstance(items, dict) else {}
            param_schema["items"] = {**base, **scalar}
    # JSON Schema 2020-12: `examples` is an array on the schema object.
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
