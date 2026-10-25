"""Parameter markers — declarative bindings for query, path, body, and header values.

Lives at the top level, not under `routing`, and imports nothing from veloce:
half the framework needs these classes, and reaching them through the `routing`
package would initialise `router`, which needs `_handler_plan`. The markers stay
public through `veloce.__all__` and `veloce.routing`, so there is exactly one
class object per marker.

Usage::

    @app.get("/items")
    async def list_items(
        q: str = Query(default="", description="Search query"),
        page: int = Query(default=1, ge=1),
        x_token: str = Header(alias="x-token"),
        session_id: str = Cookie(default=None),
    ):
        ...

    @app.post("/items")
    async def create_item(
        item_id: int = Path(description="Item ID", ge=1),
        payload: str = Body(description="Raw body"),
        name: str = Form(description="Item name"),
        file: UploadFile = File(description="Upload file"),
    ):
        ...
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import enum
import functools
import inspect
import pathlib
import re
import types
import uuid
import zoneinfo
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Any, cast, get_args

from pydantic.fields import FieldInfo
from typing_extensions import Doc, Self

#: Constraint keywords a pydantic `Field` carries that a marker models too. Read
#: by attribute name off `FieldInfo.metadata`, not by matching its classes.
_FIELD_CONSTRAINTS = (
    "ge",
    "le",
    "gt",
    "lt",
    "min_length",
    "max_length",
    "multiple_of",
    "pattern",
)

#: Bounds a `Field` can declare that a marker has nowhere to put, so the author
#: is told rather than left to find out from a 200.
_UNCARRIED_FIELD_CONSTRAINTS = (
    "max_digits",
    "decimal_places",
    "allow_inf_nan",
)


#: Types with no mutable state, so one instance serves every request. Matched
#: by exact type: a subclass can hold state its base cannot.
_ATOMIC_TYPES = frozenset(
    {
        str,
        bytes,
        int,
        float,
        complex,
        bool,
        type(None),
        range,
        datetime.date,
        datetime.timedelta,
        datetime.timezone,
        Decimal,
        uuid.UUID,
        Fraction,
        pathlib.PurePosixPath,
        pathlib.PureWindowsPath,
        pathlib.PosixPath,
        pathlib.WindowsPath,
        # Cached per key, and a deep copy returns it, so a copy isolates nothing.
        zoneinfo.ZoneInfo,
    }
)

#: How deep `is_immutable` looks before answering "cannot vouch for it";
#: reaching the bound costs a copy, where recursing cost a `RecursionError`.
_MAX_IMMUTABLE_DEPTH = 20


#: Stands in for an attribute whose read raised. A bare object is judged
#: mutable, so it fails the immutability walk by itself.
_UNREADABLE = object()


def _slot_descriptors(kind: type[Any]) -> list[types.MemberDescriptorType]:
    """Every slot descriptor in `kind`'s ancestry.

    The descriptors themselves, not names read out of `__slots__`: a private
    name is stored mangled, and a descriptor is the only way to reach a slot a
    subclass has shadowed with a class attribute of the same name.
    """
    return [
        attribute
        for klass in kind.__mro__
        for attribute in vars(klass).values()
        if isinstance(attribute, types.MemberDescriptorType)
    ]


def _guarded(read: Callable[[], Any]) -> Any:
    """Perform `read`; `None` when the attribute is absent, a sentinel when it raises.

    A check that exists to keep requests safe must not be the thing that stops
    a route being registered, so an unreadable attribute is simply not vouched for.
    """
    try:
        return read()
    except AttributeError:
        # An unset slot holds nothing, so there is nothing to share.
        return None
    except Exception:
        return _UNREADABLE


def _frozen_dataclass_is_immutable(value: Any, kind: type[Any], depth: int) -> bool:
    """Whether a frozen dataclass instance can be handed to every request.

    `frozen` stops a field being rebound, not a list in one being appended to,
    so a class that caches into its instance is refused outright. Everything
    reachable is read - the declared fields, the instance dictionary and every
    slot in the ancestry - because any one alone leaves a way to hide a list.
    """
    if any(
        isinstance(attribute, functools.cached_property)
        for klass in kind.__mro__
        for attribute in vars(klass).values()
    ):
        return False
    reachable = [
        _guarded(functools.partial(getattr, value, field.name))
        for field in dataclasses.fields(kind)
    ]
    # Through the descriptor: a same-named class attribute shadows the slot.
    reachable.extend(
        _guarded(functools.partial(descriptor.__get__, value, kind))
        for descriptor in _slot_descriptors(kind)
    )
    # Guarded too: a `__getattr__` is consulted for `__dict__` when there is none.
    state = _guarded(functools.partial(getattr, value, "__dict__"))
    if state is _UNREADABLE:
        return False
    if state is not None:
        reachable.extend(state.values())
    return all(is_immutable(member, depth + 1) for member in reachable)


def is_immutable(value: Any, depth: int = 0) -> bool:
    """True when nothing reachable through `value` can be mutated.

    Hashability is not this question - `object()` hashes and carries mutable
    attributes - and neither is `isinstance`, so an atomic type matches exactly.
    Every uncertain answer is `False`, which costs a per-request copy and stays
    correct. The one widening is the `Enum` member a deep copy hands back.
    """
    kind = type(value)
    if kind in _ATOMIC_TYPES:
        return True
    if isinstance(value, enum.Enum):
        return True
    if depth >= _MAX_IMMUTABLE_DEPTH:
        return False
    if kind is datetime.datetime or kind is datetime.time:
        # An aware value reaches its `tzinfo`, an ordinary mutable object.
        return is_immutable(value.tzinfo, depth + 1)
    if kind is tuple or kind is frozenset:
        return all(is_immutable(member, depth + 1) for member in value)
    if getattr(_guarded(functools.partial(getattr, kind, "__dataclass_params__")), "frozen", False):
        try:
            return _frozen_dataclass_is_immutable(value, kind, depth)
        except Exception:
            # Unreadable is not vouched for; it must not block registration.
            return False
    return False


def needs_per_request_copy(value: Any) -> bool:
    """True when handing `value` to every request would share mutable state.

    Containers only, matched by `isinstance` because a `list` subclass is one
    too. A default that is some other mutable object - a client, a model
    instance - is handed out as written, since a container is what a handler
    mutates in place.
    """
    if isinstance(value, (list, dict, set)):
        return True
    if isinstance(value, (tuple, frozenset)):
        return not is_immutable(value)
    return False


#: Frames of stack held in reserve while the trial copy runs: registration's
#: stack is shallower than a request's, so the trial runs this much deeper.
_COPY_STACK_MARGIN = 120


def _deepcopy_below_margin(value: Any, frames: int) -> None:
    """Deep-copy `value` with `frames` of this function's own stack beneath it."""
    if frames:
        _deepcopy_below_margin(value, frames - 1)
        return
    copy.deepcopy(value)


def _frames_available(frames: int) -> bool:
    """Whether `frames` further frames of stack can be used from here."""
    if frames:
        return _frames_available(frames - 1)
    return True


def deepcopyable(value: Any) -> bool:
    """True when `value` can be deep-copied, so every request can get its own.

    A member holding a lock or a socket cannot be, and raising from the factory
    would turn that into a 500 on every request that omits the parameter.
    """
    try:
        margin = _frames_available(_COPY_STACK_MARGIN)
    except RecursionError:
        # No margin to hold back; refusing would share an ordinary default.
        margin = False
    try:
        if margin:
            _deepcopy_below_margin(value, _COPY_STACK_MARGIN)
        else:
            copy.deepcopy(value)
    except Exception:
        return False
    return True


def _factory_wants_an_argument(factory: Any) -> bool:
    """Whether `factory` requires a positional argument, so a marker cannot call it."""
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    return any(
        p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        for p in sig.parameters.values()
    )


def field_info_uncarried_constraints(field_info: Any) -> tuple[str, ...]:
    """Return the bounds `field_info` declares that a parameter marker cannot carry."""
    if not isinstance(field_info, FieldInfo):
        return ()
    uncarried = [
        name
        for name in _UNCARRIED_FIELD_CONSTRAINTS
        if any(getattr(meta, name, None) is not None for meta in field_info.metadata)
    ]
    # A marker has one string to express a lookup with, so only a string folds.
    validation_alias = getattr(field_info, "validation_alias", None)
    if validation_alias is not None and not isinstance(validation_alias, str):
        uncarried.append("validation_alias")
    return tuple(uncarried)


def field_info_declares_constraints(field_info: Any) -> bool:
    """Whether a `Field` declares a bound, rather than only documentation.

    `Field(description=...)` drops nothing enforceable, so the "constraints are
    ignored" diagnostics must not fire on it. False for anything that is not a
    `FieldInfo`, so a caller can run it over a whole metadata tuple.
    """
    if not isinstance(field_info, FieldInfo):
        return False
    return any(
        getattr(meta, name, None) is not None
        for meta in field_info.metadata
        for name in _FIELD_CONSTRAINTS
    )


class ParamBase:
    """Base class for parameter markers."""

    __slots__ = (
        "default",
        "default_factory",
        "alias",
        "title",
        "description",
        "ge",
        "le",
        "gt",
        "lt",
        "min_length",
        "max_length",
        "multiple_of",
        "regex",
        "_regex_compiled",
        "deprecated",
        "examples",
        "embed",
        "convert_underscores",
        "group",
        "include_in_schema",
    )

    def __init__(
        self,
        default: Annotated[
            Any,
            Doc(
                "Static fallback value used when the parameter is absent; `...` marks it required."
            ),
        ] = ...,
        default_factory: Annotated[
            Callable[[], Any] | None,
            Doc(
                "Callable invoked per request to build a fresh default, avoiding shared mutable state."
            ),
        ] = None,
        alias: Annotated[
            str | None,
            Doc("External name to read the value under instead of the Python parameter name."),
        ] = None,
        title: Annotated[
            str | None,
            Doc("Human-readable title emitted into the parameter's OpenAPI schema."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("Description emitted into the parameter's OpenAPI schema."),
        ] = None,
        ge: Annotated[
            float | None,
            Doc("Require the numeric value to be greater than or equal to this bound."),
        ] = None,
        le: Annotated[
            float | None,
            Doc("Require the numeric value to be less than or equal to this bound."),
        ] = None,
        gt: Annotated[
            float | None,
            Doc("Require the numeric value to be strictly greater than this bound."),
        ] = None,
        lt: Annotated[
            float | None,
            Doc("Require the numeric value to be strictly less than this bound."),
        ] = None,
        min_length: Annotated[
            int | None,
            Doc("Require the string value to have at least this many characters."),
        ] = None,
        max_length: Annotated[
            int | None,
            Doc("Require the string value to have at most this many characters."),
        ] = None,
        multiple_of: Annotated[
            float | None,
            Doc("Require the numeric value to be a multiple of this positive number."),
        ] = None,
        regex: Annotated[
            str | None,
            Doc("Deprecated alias for `pattern`; the regular expression the value must match."),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc(
                "Regular expression the value must match, searched as JSON Schema's "
                "`pattern` is - anchor with `^...$` to require a full match. Takes "
                "precedence over `regex`."
            ),
        ] = None,
        deprecated: Annotated[
            bool,
            Doc("Mark the parameter as deprecated in the OpenAPI schema."),
        ] = False,
        examples: Annotated[
            list[Any] | None,
            Doc("Example values emitted into the parameter's OpenAPI schema."),
        ] = None,
        embed: Annotated[
            bool,
            Doc(
                "For `Body` markers, nest the value under the parameter name inside the JSON body."
            ),
        ] = False,
        convert_underscores: Annotated[
            bool,
            Doc(
                "For `Header` markers, rewrite underscores in the name to hyphens (`x_token` to `x-token`)."
            ),
        ] = True,
        group: Annotated[
            bool,
            Doc(
                "Read a model annotation's fields from this source instead of one "
                "key holding a JSON document (`Annotated[Filters, Query(group=True)]`)."
            ),
        ] = False,
        include_in_schema: Annotated[
            bool,
            Doc(
                "Resolve the parameter at runtime but omit it from the generated OpenAPI document."
            ),
        ] = True,
    ) -> None:
        # Mutually exclusive: the factory exists to build a fresh value.
        if default is not ... and default_factory is not None:
            raise ValueError("default and default_factory are mutually exclusive")
        self.default = default
        # A static mutable default is one list handed out by identity, so an
        # in-place mutation by one request is visible to the next.
        if default_factory is None and needs_per_request_copy(default) and deepcopyable(default):
            default_factory = functools.partial(copy.deepcopy, default)
        self.default_factory = default_factory
        self.alias = alias
        self.title = title
        self.description = description
        self.ge = ge
        self.le = le
        self.gt = gt
        self.lt = lt
        self.min_length = min_length
        self.max_length = max_length
        # JSON Schema draft 2020-12 Sec. 6.2.1 requires `multipleOf` > 0.
        if multiple_of is not None and multiple_of <= 0:
            raise ValueError("multiple_of must be positive")
        self.multiple_of = multiple_of
        # The renamed `regex` -> `pattern`; `pattern` wins when both are given.
        self.regex = pattern if pattern is not None else regex
        # Compiled here, so per-request `validate` does no `re` cache lookup.
        self._regex_compiled = re.compile(self.regex) if self.regex is not None else None
        self.deprecated = deprecated
        self.examples = examples
        self.embed = embed
        self.convert_underscores = convert_underscores
        # Off by default: a bare model annotation already means one JSON key.
        self.group = group
        self.include_in_schema = include_in_schema

    @classmethod
    def from_field_info(cls, field_info: FieldInfo, default: Any = ...) -> Self:
        """Build this marker from a pydantic `Field` carried in `Annotated` metadata.

        Folding it into a marker is what makes it enforced and published: the
        slot, the OpenAPI document and the MCP tool schema all read the marker
        and none of them knows about `FieldInfo`. `default` is the parameter's
        own, adopted only when the `Field` declares none.
        """
        constraints: dict[str, Any] = {}
        for meta in field_info.metadata:
            for name in _FIELD_CONSTRAINTS:
                value = getattr(meta, name, None)
                if value is not None:
                    constraints[name] = value
        pattern = constraints.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            # The published schema needs the source text, not a `Pattern` repr.
            constraints["pattern"] = pattern.pattern
        if field_info.default_factory is not None:
            # A marker calls the factory with no arguments; pydantic >= 2.10
            # also accepts a one-argument one, which would `TypeError` per request.
            if _factory_wants_an_argument(field_info.default_factory):
                raise TypeError(
                    "Field(default_factory=...) takes the validated data, which a "
                    "parameter marker cannot supply; use a factory that takes no "
                    "arguments"
                )
            # The stronger declaration: a `Field` carrying one has no default.
            constraints["default_factory"] = field_info.default_factory
        elif not field_info.is_required():
            constraints["default"] = field_info.default
        elif default is not ...:
            constraints["default"] = default
        # `validation_alias` wins, pydantic's own precedence; a string only.
        wire_alias = field_info.alias
        validation_alias = getattr(field_info, "validation_alias", None)
        if isinstance(validation_alias, str):
            wire_alias = validation_alias
        return cls(
            alias=wire_alias,
            title=field_info.title,
            description=field_info.description,
            examples=list(field_info.examples) if field_info.examples else None,
            # May be a string explaining the replacement rather than a flag.
            deprecated=bool(getattr(field_info, "deprecated", None)),
            **constraints,
        )

    @property
    def has_default(self) -> bool:
        return self.default is not ... or self.default_factory is not None

    def with_default(self, default: Any) -> Self:
        """Return a copy of this marker carrying `default` as its own.

        A copy, because one marker can be shared between parameters -
        `SHARED = Query()` used twice must not take the first one's default.
        """
        folded = copy.copy(self)
        folded.default = default
        # The same guard `__init__` applies to a declared mutable default.
        if needs_per_request_copy(default) and deepcopyable(default):
            folded.default_factory = functools.partial(copy.deepcopy, default)
        return folded

    def resolve_default(self) -> Any:
        """Return a fresh default - calling `default_factory` when one is set."""
        if self.default_factory is not None:
            return self.default_factory()
        return self.default

    def validate(self, value: Any, name: str) -> Any:
        """Validate value against constraints."""
        if value is None and self.has_default:
            return self.resolve_default()

        if isinstance(value, (int, float, Decimal)):
            if self.ge is not None and value < self.ge:
                raise ValueError(f"{name} must be >= {self.ge}")
            if self.le is not None and value > self.le:
                raise ValueError(f"{name} must be <= {self.le}")
            if self.gt is not None and value <= self.gt:
                raise ValueError(f"{name} must be > {self.gt}")
            if self.lt is not None and value >= self.lt:
                raise ValueError(f"{name} must be < {self.lt}")
            if self.multiple_of is not None:
                quotient = float(value) / float(self.multiple_of)
                if abs(quotient - round(quotient)) > 1e-9:
                    raise ValueError(f"{name} must be a multiple of {self.multiple_of}")

        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise ValueError(f"{name} must have at least {self.min_length} characters")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"{name} must have at most {self.max_length} characters")
            # `search`, not `fullmatch`: JSON Schema's `pattern`, published
            # verbatim from here, is a partial match. Anchor with `^...$`.
            if self._regex_compiled is not None and not self._regex_compiled.search(value):
                raise ValueError(f"{name} does not match pattern {self.regex}")

        return value


class Query(ParamBase):
    """Query parameter declaration."""

    __slots__ = ()


class Path(ParamBase):
    """Path parameter declaration."""

    __slots__ = ()


class Body(ParamBase):
    """Request body parameter declaration."""

    __slots__ = ()


class Form(ParamBase):
    """Form field parameter declaration."""

    __slots__ = ()


class File(ParamBase):
    """File upload parameter declaration."""

    __slots__ = ()


class Header(ParamBase):
    """HTTP header parameter declaration."""

    __slots__ = ()


class Cookie(ParamBase):
    """Cookie parameter declaration."""

    __slots__ = ()


#: Where the constraint vocabulary pydantic understands is defined -
#: `annotated_types` plus pydantic's own, such as `StringConstraints`.
_CONSTRAINT_MODULES = frozenset({"annotated_types", "pydantic"})


def _is_constraint(meta: Any) -> bool:
    """True when pydantic reads this metadata object as a constraint.

    Identified by where its class is defined rather than by importing the
    constraint classes, so no transitive package becomes a direct dependency.
    """
    return type(meta).__module__.split(".", 1)[0] in _CONSTRAINT_MODULES


def find_field_info(annotation: Any) -> FieldInfo | None:
    """Return the pydantic `FieldInfo` an annotation declares, or `None`.

    Matched on the `FieldInfo` type rather than on the name the author typed,
    so a renamed import is recognised like any other. Several are merged rather
    than first-wins, because `Annotated` accumulates metadata and one
    parameter's constraints can arrive split across entries; the merge follows
    pydantic's own last-writer-wins rule.
    """
    metadata = getattr(annotation, "__metadata__", ())
    if not metadata:
        return None
    found = [meta for meta in metadata if isinstance(meta, FieldInfo)]
    bare = [meta for meta in metadata if not isinstance(meta, FieldInfo) and _is_constraint(meta)]
    if bare:
        # Rebuilt over an annotation carrying only the constraints, because
        # handing pydantic the original would read our own markers as metadata.
        declared = get_args(annotation)[0]
        rebuilt = cast("type[Any]", Annotated[(declared, *bare)])
        found.insert(0, FieldInfo.from_annotation(rebuilt))
    if not found:
        return None
    # `merge_field_infos` is deprecated, but the supported `Annotated` chain
    # keeps every entry where this documents last-wins.
    return found[0] if len(found) == 1 else FieldInfo.merge_field_infos(*found)
