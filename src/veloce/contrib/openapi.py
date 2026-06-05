"""OpenAPI 3.1 schema generation and Swagger UI — auto-generated from routes."""

from __future__ import annotations

import contextlib
import copy
import datetime
import enum
import html
import inspect
import logging
import types
import uuid
import weakref
from decimal import Decimal
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import orjson
from pydantic import BaseModel

from veloce._constants import MIME_FORM_URLENCODED, MIME_JSON, MIME_MULTIPART_FORM_DATA
from veloce._protocol_constants import OAUTH2_GRANT_TYPE_PASSWORD
from veloce.dependency import Depends
from veloce.http.response import HTMLResponse, JSONResponse
from veloce.routing.params import Body as BodyParam
from veloce.routing.params import Cookie as CookieParam
from veloce.routing.params import File as FileParam
from veloce.routing.params import Form as FormParam
from veloce.routing.params import Header as HeaderParam
from veloce.routing.params import ParamBase
from veloce.routing.params import Path as PathParam
from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery
from veloce.security.http import HTTPBasic, HTTPBearer, HTTPDigest
from veloce.security.oauth2 import (
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OpenIdConnect,
)
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

# Swagger UI / ReDoc bundles are pinned to a specific patch version and
# loaded with a Subresource Integrity hash. Together with
# `crossorigin="anonymous"` the browser refuses to execute the script
# if the CDN ever serves bytes that do not hash to this exact digest,
# so a CDN compromise cannot inject arbitrary JavaScript onto a
# `/docs` page. Bump the versions in lock-step with the hashes - the
# hash will not match if you change one without the other.
_SWAGGER_UI_VERSION = "5.18.2"
_SWAGGER_UI_CSS_INTEGRITY = "sha512-xRGj65XGEcpPTE7Cn6ujJWokpXVLxqLxdtNZ/n1w52+76XaCRO7UWKZl9yJHvzpk99A0EP6EW+opPcRwPDxwkA=="
_SWAGGER_UI_JS_INTEGRITY = "sha512-9tBcCofqWq+PelL6USpUB7OJrCaObfefi9ht9nVZuKt1XP7eHDs7NwVljLSLVtSsErax1Tz3pG3O82eeq546Rg=="
_REDOC_VERSION = "2.1.5"
_REDOC_JS_INTEGRITY = "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD"


# ── Introspection / merge helpers ──────────────────────────


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
            hints = get_type_hints(handler)
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
    """Return True for a Pydantic ``BaseModel`` subclass annotation."""
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


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
    # schema, not a string default. Lets `dict[str, Any]` emit
    # `additionalProperties: {}` rather than forcing string-valued entries.
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
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
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
        resolved_token = {
            t: (token_to_name.get(t) or token_to_name[self._alias_target(t)])
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
        """Return a component name derived from `base` not already in use."""
        # The `-Output` suffix mirrors the top-level serialization variant
        # naming so a diverging nested model reads as its owner's output twin.
        candidate = f"{base}-Output"
        if candidate not in components:
            return candidate
        n = 2
        while f"{candidate}_{n}" in components:
            n += 1
        return f"{candidate}_{n}"

    def _alias_target(self, token: str) -> str:
        for entry in self._entries.values():
            if entry.token == token and entry.alias_token is not None:
                return entry.alias_token
        return token

    def _all_tokens(self) -> list[str]:
        return [self._entries[k].token for k in self._order]


def _rewrite_byte_format(node: Any) -> None:
    """Rewrite bytes string fields from OpenAPI `format: binary` to `format: byte`.

    Veloce's JSON encoder base64-encodes `bytes`/`bytearray`, so a bytes field in
    a JSON model travels as a base64 string - RFC 4648 `byte` - not raw `binary`.
    Pydantic emits `binary` for `bytes`; this realigns the generated schema with
    the actual serialized form. Walks nested objects, arrays, and `$defs` in place.
    """
    if isinstance(node, dict):
        if node.get("type") == "string" and node.get("format") == "binary":
            node["format"] = "byte"
        for value in node.values():
            _rewrite_byte_format(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_byte_format(item)


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
        try:
            schema = self.model.model_json_schema(mode=self.mode)  # type: ignore[arg-type]
        except Exception as exc:
            # Degrading silently to `{type: object}` hides real model bugs.
            # Log at WARNING so the failure surfaces, then fall back so /docs
            # still renders (an underspecified schema beats a 500).
            _logger.warning(
                "OpenAPI schema generation failed for %s (%s mode): %s. "
                "Falling back to {type: object}. "
                "Inspect the model definition or attach a debugger to "
                "veloce.contrib.openapi to see the full traceback.",
                self.model.__name__,
                self.mode,
                exc,
                exc_info=_logger.isEnabledFor(logging.DEBUG),
            )
            self.body = {"type": "object"}
            return
        # Realign bytes fields with veloce's base64 JSON encoding (binary -> byte).
        _rewrite_byte_format(schema)
        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                self.defs[def_name] = def_schema
            del schema["$defs"]
        self.body = schema


def _local_def_refs(node: Any) -> set[str]:
    """Collect every `#/$defs/<name>` target referenced anywhere under `node`."""
    targets: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith(_PYDANTIC_DEF_PREFIX):
                targets.add(ref[len(_PYDANTIC_DEF_PREFIX) :])
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(node)
    return targets


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
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_PYDANTIC_DEF_PREFIX):
            old = ref[len(_PYDANTIC_DEF_PREFIX) :]
            new = renames.get(old)
            if new is not None:
                node["$ref"] = f"#/components/schemas/{new}"
        for value in node.values():
            _rewrite_local_defs(value, renames)
    elif isinstance(node, list):
        for item in node:
            _rewrite_local_defs(item, renames)


def _rewrite_refs(node: Any, token_to_name: dict[str, str]) -> None:
    """Rewrite placeholder and `#/$defs/...` `$ref`s in place throughout `node`."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith(_REF_PLACEHOLDER_PREFIX):
                name = token_to_name.get(ref[len(_REF_PLACEHOLDER_PREFIX) :])
                if name is not None:
                    node["$ref"] = f"#/components/schemas/{name}"
            elif ref.startswith(_PYDANTIC_DEF_PREFIX):
                node["$ref"] = f"#/components/schemas/{ref[len(_PYDANTIC_DEF_PREFIX) :]}"
        for value in node.values():
            _rewrite_refs(value, token_to_name)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item, token_to_name)


def _pydantic_to_schema(model: type[BaseModel], registry: dict[str, dict]) -> dict:
    """Convert a Pydantic model to a name-keyed `$ref`, extending `registry`.

    Standalone validation-mode renderer for callers that build a self-contained
    schema envelope (the MCP plan bridge inlines these into per-tool `$defs`).
    The document-wide OpenAPI generator uses `SchemaRegistry` instead, which
    keys on class identity and supports dual validation/serialization modes.
    """
    name = model.__name__
    if name not in registry:
        try:
            schema = model.model_json_schema()
            _rewrite_byte_format(schema)
            if "$defs" in schema:
                for def_name, def_schema in schema["$defs"].items():
                    registry[def_name] = def_schema
                del schema["$defs"]
            registry[name] = schema
        except Exception as exc:
            _logger.warning(
                "OpenAPI schema generation failed for %s: %s. "
                "Falling back to {type: object}. "
                "Inspect the model definition or attach a debugger to "
                "veloce.contrib.openapi to see the full traceback.",
                name,
                exc,
                exc_info=_logger.isEnabledFor(logging.DEBUG),
            )
            registry[name] = {"type": "object"}
    return {"$ref": f"#/components/schemas/{name}"}


def _response_model_to_schema(response_model: Any, registry: SchemaRegistry) -> dict | None:
    """Render `response_model` into an OpenAPI schema object.

    Handles three shapes:
    - `MyModel` (Pydantic BaseModel subclass) -> `{"$ref": ".../MyModel"}`.
    - `list[MyModel]` (or any `Sequence[MyModel]`) -> array-of-refs.
    - Anything else -> `None` (caller omits the schema).

    Response models render under the model's serialization JSON Schema so
    computed / read-only fields are documented as clients actually receive them.
    """
    origin = get_origin(response_model)
    if origin is list:
        args = get_args(response_model)
        if args and _is_model_type(args[0]):
            inner = registry.ref(args[0], mode="serialization")
            return {"type": "array", "items": inner}
        return {"type": "array", "items": {}}

    if _is_model_type(response_model):
        return registry.ref(response_model, mode="serialization")

    return None


# ── Info / parameter / body / response builders ────────────


def _build_info_object(app: Any) -> dict[str, Any]:
    """Return the OpenAPI `info` object assembled from app metadata."""
    info_obj: dict[str, Any] = {
        "title": getattr(app, "title", "Veloce API"),
        "version": getattr(app, "version", "0.1.0"),
    }
    if getattr(app, "summary", None):
        info_obj["summary"] = app.summary
    if getattr(app, "description", ""):
        info_obj["description"] = app.description
    if getattr(app, "terms_of_service", None):
        info_obj["termsOfService"] = app.terms_of_service
    if getattr(app, "contact", None):
        info_obj["contact"] = app.contact
    if getattr(app, "license_info", None):
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


def _extract_parameters(
    info: Any, schemas_registry: SchemaRegistry
) -> tuple[list[dict], dict | None, list[tuple[str, dict, bool, bool]]]:
    """Walk the handler signature and classify every parameter.

    Returns `(parameters, request_body_schema, form_fields)`:
    - `parameters` - OpenAPI parameter objects for path/query/header/cookie.
    - `request_body_schema` - schema of the first Pydantic body model (or None).
    - `form_fields` - `(alias, schema, required, is_file)` tuples for
      `Form()` / `File()` params, consumed by `_extract_request_body`.

    Depends/Security and `Body()` markers are intentionally dropped here -
    they belong to other parts of the operation object.
    """
    handler = info.handler
    sig, hints = _handler_intro(handler)
    parameters: list[dict] = []
    request_body_schema: dict | None = None
    form_fields: list[tuple[str, dict, bool, bool]] = []

    if sig is None:
        return parameters, request_body_schema, form_fields

    for pname, param in sig.parameters.items():
        if pname in ("self", "request"):
            continue
        if isinstance(param.default, Depends):
            continue

        annotation = hints.get(pname)

        marker = None
        if isinstance(param.default, ParamBase):
            marker = param.default
            # `include_in_schema=False` - resolved at runtime but omitted.
            if not getattr(marker, "include_in_schema", True):
                continue

        # A BaseModel-typed param becomes the JSON request body only when it is
        # NOT pinned to a non-body source by a marker. A bare model (no marker)
        # or one with an explicit `Body()` is a JSON body; a model carried by a
        # `Query`/`Header`/`Cookie`/`Form`/`File` marker is read from that source
        # as a JSON-document string at runtime, so it belongs in `parameters` /
        # form fields (where it emits `{"type": "string"}`), not `requestBody`.
        if (
            annotation
            and _is_model_type(annotation)
            and (marker is None or isinstance(marker, BodyParam))
        ):
            request_body_schema = schemas_registry.ref(annotation, mode="validation")
            continue

        # Determine parameter location.
        if marker and isinstance(marker, HeaderParam):
            param_location = "header"
            if marker.alias:
                param_alias = marker.alias
            elif getattr(marker, "convert_underscores", True):
                # Mirror the runtime header lookup in _handler_plan: an
                # un-aliased header param's `_` is read off the wire as `-`,
                # so document the hyphenated name the resolver matches.
                param_alias = pname.replace("_", "-")
            else:
                param_alias = pname
        elif marker and isinstance(marker, CookieParam):
            param_location = "cookie"
            param_alias = marker.alias or pname
        elif marker and isinstance(marker, (FormParam, FileParam)):
            is_file = isinstance(marker, FileParam)
            if is_file:
                field_schema: dict[str, Any] = {"type": "string", "format": "binary"}
            else:
                field_schema = _python_type_to_schema(annotation)
            if marker.description:
                field_schema["description"] = marker.description
            if getattr(marker, "title", None):
                field_schema["title"] = marker.title
            field_required = not marker.has_default
            field_alias = marker.alias or pname
            form_fields.append((field_alias, field_schema, field_required, is_file))
            continue
        elif marker and isinstance(marker, BodyParam):
            # Body goes into requestBody, not parameters.
            continue
        elif pname in info.param_names or (marker and isinstance(marker, PathParam)):
            param_location = "path"
            param_alias = pname
        else:
            param_location = "query"
            param_alias = marker.alias if marker and marker.alias else pname

        param_schema = _python_type_to_schema(annotation)

        if marker:
            _apply_marker_constraints(param_schema, marker)

        if marker:
            required = not marker.has_default
            if marker.has_default and marker.default is not ...:
                default_val = marker.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val
        elif param_location == "path":
            required = True
        else:
            required = param.default is inspect.Parameter.empty
            if not required and param.default is not inspect.Parameter.empty:
                default_val = param.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val

        param_info: dict[str, Any] = {
            "name": param_alias,
            "in": param_location,
            "required": required,
            "schema": param_schema,
        }

        # OpenAPI 3.1 Sec. 4.8.12.1 - array-valued query parameters default
        # to `style: form`, `explode: true` (one `?k=v1&k=v2` per item).
        if param_location == "query" and param_schema.get("type") == "array":
            param_info["style"] = "form"
            param_info["explode"] = True

        if marker and marker.deprecated:
            param_info["deprecated"] = True

        parameters.append(param_info)

    return parameters, request_body_schema, form_fields


def _extract_request_body(
    request_body_schema: dict | None,
    form_fields: list[tuple[str, dict, bool, bool]],
) -> dict | None:
    """Build the OpenAPI `requestBody` object, or `None` when no body params exist.

    A JSON Pydantic body takes precedence over form fields, matching the
    monolithic implementation. When only form fields are present, the
    media type is `multipart/form-data` if any field is a file upload
    (OpenAPI 3.1 Sec. 4.8.10.4), otherwise `application/x-www-form-urlencoded`.
    """
    if request_body_schema:
        return {
            "required": True,
            "content": {MIME_JSON: {"schema": request_body_schema}},
        }
    if not form_fields:
        return None
    has_file = any(is_file for _, _, _, is_file in form_fields)
    media_type = MIME_MULTIPART_FORM_DATA if has_file else MIME_FORM_URLENCODED
    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    for fname, fschema, freq, _ in form_fields:
        properties[fname] = fschema
        if freq:
            required_fields.append(fname)
    body_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required_fields:
        body_schema["required"] = required_fields
    return {
        "required": True,
        "content": {media_type: {"schema": body_schema}},
    }


# Component-schema names for the auto-generated validation-error response. The
# `{"detail": [{"loc", "msg", "type"}, ...]}` payload these describe is exactly
# what `request_validation_exception_handler` renders for a 422, so the document
# advertises the body the resolver actually returns when a path/query/header/
# cookie/body/form parameter fails validation.
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
            }
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
        resp_schema = _response_model_to_schema(info.response_model, schemas_registry)
        if resp_schema is not None:
            responses[primary_status]["content"] = {MIME_JSON: {"schema": resp_schema}}

    # Auto-add the validation-error response for operations whose request is
    # validated. Skipped when the user already declares a 422 - via `responses=`
    # OR `openapi_extra={"responses": {"422": ...}}` (which is deep-merged onto
    # the operation later) - so a custom 422 shape / media type is preserved
    # rather than overwritten. Operations with no validatable parameter never
    # advertise a 422 the resolver cannot raise.
    _openapi_extra_responses = (getattr(info, "openapi_extra", None) or {}).get("responses") or {}
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

    Returns `None` for unknown scheme classes. The name is derived from the
    scheme class so duplicate registrations of the same scheme reuse the same
    `components.securitySchemes` entry.
    """
    cls_name = type(scheme).__name__
    if isinstance(scheme, OAuth2PasswordBearer):
        return cls_name, {
            "type": "oauth2",
            "flows": {
                OAUTH2_GRANT_TYPE_PASSWORD: {
                    "tokenUrl": getattr(scheme, "token_url", ""),
                    "scopes": getattr(scheme, "scopes", {}) or {},
                }
            },
        }
    if isinstance(scheme, OAuth2AuthorizationCodeBearer):
        flow: dict[str, Any] = {
            "authorizationUrl": getattr(scheme, "authorizationUrl", ""),
            "tokenUrl": getattr(scheme, "tokenUrl", ""),
            "scopes": getattr(scheme, "scopes", {}) or {},
        }
        refresh = getattr(scheme, "refreshUrl", None)
        if refresh:
            flow["refreshUrl"] = refresh
        return cls_name, {
            "type": "oauth2",
            "flows": {"authorizationCode": flow},
        }
    if isinstance(scheme, OpenIdConnect):
        return cls_name, {
            "type": "openIdConnect",
            "openIdConnectUrl": getattr(scheme, "openIdConnectUrl", ""),
        }
    if isinstance(scheme, HTTPBearer):
        return cls_name, {
            "type": "http",
            "scheme": "bearer",
        }
    if isinstance(scheme, HTTPBasic):
        return cls_name, {
            "type": "http",
            "scheme": "basic",
        }
    if isinstance(scheme, HTTPDigest):
        return cls_name, {
            "type": "http",
            "scheme": "digest",
        }
    if isinstance(scheme, APIKeyHeader):
        return cls_name, {
            "type": "apiKey",
            "in": "header",
            "name": getattr(scheme, "name", ""),
        }
    if isinstance(scheme, APIKeyQuery):
        return cls_name, {
            "type": "apiKey",
            "in": "query",
            "name": getattr(scheme, "name", ""),
        }
    if isinstance(scheme, APIKeyCookie):
        return cls_name, {
            "type": "apiKey",
            "in": "cookie",
            "name": getattr(scheme, "name", ""),
        }
    return None


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
            # Generic dep - recurse into its handler signature.
            inner = target
        else:
            inner = dep

        if inner is None or id(inner) in seen:
            return
        seen.add(id(inner))

        sig, _ = _handler_intro(inner)
        if sig is None:
            return
        for param in sig.parameters.values():
            default = param.default
            if isinstance(default, Depends):
                visit(default)

    # Route-level dependencies (the `dependencies=[Depends(...)]` kwarg).
    for d in getattr(info, "dependencies", ()) or ():
        visit(d)
    # Plus anything in the handler's own parameter defaults.
    handler = info.handler
    sig, _ = _handler_intro(handler)
    if sig is None:
        return requirements
    for param in sig.parameters.values():
        default = param.default
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
        sig, hints = _handler_intro(dep_callable)
        if sig is None:
            return False
        for param in sig.parameters.values():
            default = param.default
            if isinstance(default, Depends):
                target = default.dependency
                if _scheme_definition(target) is not None:
                    continue
                if visit(target):
                    return True
                continue
            if isinstance(default, ParamBase):
                return True
            if _is_model_type(hints.get(param.name, param.annotation)):
                return True
        return False

    for dep in getattr(info, "dependencies", None) or []:
        if isinstance(dep, Depends):
            target = dep.dependency
            if _scheme_definition(target) is None and visit(target):
                return True
    return visit(getattr(info, "handler", None))


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
    op_id = (
        info.operation_id if getattr(info, "operation_id", None) else f"{info.name}_{method_lower}"
    )
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
    if getattr(info, "callbacks", None):
        operation["callbacks"] = info.callbacks

    parameters, request_body_schema, form_fields = _extract_parameters(info, schemas_registry)
    if parameters:
        operation["parameters"] = parameters

    request_body = _extract_request_body(request_body_schema, form_fields)
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
        bool(parameters) or request_body is not None or _dependency_graph_has_validatable(info)
    )
    operation["responses"] = _extract_responses(info, schemas_registry, has_validatable_params)

    # `openapi_extra` - deep-merge the user-supplied dict over the
    # generated operation. Nested dicts merge key-by-key; scalars and
    # lists in `openapi_extra` overwrite.
    extra = getattr(info, "openapi_extra", None)
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
    webhooks_router = getattr(app, "webhooks", None)
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
    schemas = schema.get("components", {}).get("schemas", {})
    paths = schema.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document `paths` must be an object")

    known_refs = {f"#/components/schemas/{name}" for name in schemas}

    def check_refs(node: Any, where: str) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith("#/components/schemas/")
                and ref not in known_refs
            ):
                raise ValueError(f"{where}: unresolved schema $ref {ref!r}")
            for value in node.values():
                check_refs(value, where)
        elif isinstance(node, list):
            for item in node:
                check_refs(item, where)

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


def get_openapi_schema(app: Any) -> dict:
    """Generate OpenAPI 3.1 schema from the app's registered routes."""
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": _build_info_object(app),
        "paths": {},
        "components": {"schemas": {}},
    }

    if getattr(app, "servers", None):
        schema["servers"] = app.servers
    if getattr(app, "openapi_tags", None):
        schema["tags"] = app.openapi_tags
    # OpenAPI 3.1 Sec. 4.8.11 - top-level `externalDocs` object.
    if getattr(app, "openapi_external_docs", None):
        schema["externalDocs"] = app.openapi_external_docs

    schemas_registry = SchemaRegistry(
        separate_input_output=getattr(app, "separate_input_output_schemas", True)
    )
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

    for method, path, info in app._collect_all_routes():
        method_lower = method.lower()
        if path not in schema["paths"]:
            schema["paths"][path] = {}
        operation = _build_operation(
            info, method_lower, schemas_registry, security_schemes_registry
        )
        if _references_validation_error_schema(operation):
            needs_validation_error_schema = True
        schema["paths"][path][method_lower] = operation
        if getattr(info, "operation_id", None):
            explicit_ops.append((operation, path, method_lower))
        else:
            auto_ops.append((operation, path, method_lower))

    # Webhook operations are appended to `auto_ops` so the disambiguation pass
    # below dedupes operationIds across BOTH routes and webhooks deterministically
    # (routes first in collection order, then webhooks in walker order).
    webhook_items = _walk_webhooks(app, schemas_registry, auto_ops)
    if webhook_items:
        schema["webhooks"] = webhook_items

    if getattr(app, "disambiguate_operation_ids", True):
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

    validate = getattr(app, "validate_openapi", None)
    if validate is None:
        validate = bool(getattr(app, "debug", False))
    if validate:
        _validate_document(schema)

    return schema


# ── Swagger UI / ReDoc templates ───────────────────────────


# Byte-level escapes applied to orjson output before it is embedded inline in
# a <script> block: the close-script breakout (`<`/`>`/`&`) and the U+2028 /
# U+2029 line separators (valid in JSON, but break JS string literals). The
# escapes are JSON-valid `\uXXXX` forms, so SwaggerUIBundle / JSON.parse read
# them identically.
_SCRIPT_ESCAPES = (
    (b"<", b"\\u003c"),
    (b">", b"\\u003e"),
    (b"&", b"\\u0026"),
    (b"\xe2\x80\xa8", b"\\u2028"),
    (b"\xe2\x80\xa9", b"\\u2029"),
)


def _html_safe_orjson(value: Any) -> str:
    """Serialise `value` to JSON safe to embed inline in a <script> block."""
    raw = orjson.dumps(value)
    for needle, repl in _SCRIPT_ESCAPES:
        if needle in raw:
            raw = raw.replace(needle, repl)
    return raw.decode()


SWAGGER_HTML = (
    """<!DOCTYPE html>
<html>
<head>
    <title>{title} - Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link
      rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui.min.css"
      integrity="__SUC__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer">
</head>
<body>
    <div id="swagger-ui"></div>
    <script
      src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui-bundle.min.js"
      integrity="__SUJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"></script>
    <script>
    const ui = SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "StandaloneLayout",
        {ui_params}
    }});
    {init_oauth}
    </script>
</body>
</html>""".replace("__SUV__", _SWAGGER_UI_VERSION)
    .replace("__SUC__", _SWAGGER_UI_CSS_INTEGRITY)
    .replace("__SUJ__", _SWAGGER_UI_JS_INTEGRITY)
)


REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} - ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
    <redoc spec-url='{openapi_url}'></redoc>
    <script
      src="https://unpkg.com/redoc@__RDV__/bundles/redoc.standalone.js"
      integrity="__RDJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"></script>
</body>
</html>""".replace("__RDV__", _REDOC_VERSION).replace("__RDJ__", _REDOC_JS_INTEGRITY)


def setup_openapi_routes(
    app: Any,
    openapi_url: str = "/openapi.json",
    docs_url: str | None = "/docs",
    redoc_url: str | None = "/redoc",
) -> None:
    """Register OpenAPI schema and documentation routes.

    `docs_url` / `redoc_url` of `None` disable the Swagger UI / ReDoc UI
    respectively - the JSON schema route is still registered, so tooling
    can consume the schema without a public interactive explorer.
    """

    @app.get(openapi_url, tags=["openapi"], name="openapi_schema")
    async def openapi_schema(request: Any):
        # Route through `app.openapi()` so a user override / customised
        # `app.openapi_schema` flows to the JSON endpoint and Swagger UI.
        return JSONResponse(app.openapi())

    async def swagger_ui(request: Any):
        # Render extra SwaggerUIBundle options inline as JSON literals.
        # `orjson.dumps` returns bytes, so decode for string concatenation
        # into the HTML template; the surrounding page is utf-8, so
        # orjson's raw-UTF-8 output (vs json's ensure_ascii) is fine.
        params = getattr(app, "swagger_ui_parameters", None) or {}
        if params:
            # Compact `key:value` join - orjson serialises nested values
            # without spaces, so the outer separator stays spaceless to
            # keep the rendered literal consistent throughout.
            ui_params = ",".join(
                f"{_html_safe_orjson(k)}:{_html_safe_orjson(v)}" for k, v in params.items()
            )
        else:
            ui_params = ""

        oauth_init = getattr(app, "swagger_ui_init_oauth", None)
        init_oauth = f"ui.initOAuth({_html_safe_orjson(oauth_init)});" if oauth_init else ""

        html_page = SWAGGER_HTML.format(
            title=html.escape(app.title),
            openapi_url=html.escape(openapi_url),
            ui_params=ui_params,
            init_oauth=init_oauth,
        )
        return HTMLResponse(html_page)

    async def redoc_ui(request: Any):
        html_page = REDOC_HTML.format(
            title=html.escape(app.title), openapi_url=html.escape(openapi_url)
        )
        return HTMLResponse(html_page)

    # Register each interactive UI only when its URL is set - a `None`
    # disables that UI while leaving the JSON schema route in place.
    if docs_url is not None:
        app.get(docs_url, tags=["openapi"], name="swagger_ui")(swagger_ui)
    if redoc_url is not None:
        app.get(redoc_url, tags=["openapi"], name="redoc_ui")(redoc_ui)
