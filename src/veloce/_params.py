"""Parameter markers — declarative bindings for query, path, body, and header values.

Lives at the top level, not under `routing`, and imports nothing from veloce.
Half the framework needs these classes - `_handler_plan`, `views`,
`contrib.openapi`, `security.oauth2` - and importing them from inside the
`routing` package meant initialising it, which imports `router`, which needs
`_handler_plan`. That was a real cycle, worked around by a deferred import.
Keeping the markers on a neutral leaf removes the cycle instead of deferring it.

The markers stay public through `veloce.__all__` and `veloce.routing`, so there is
exactly one class object per marker and no module exists merely to redirect to
this one.

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

#: Constraint keywords a pydantic `Field` carries that a marker models too.
#: They are read by attribute name off each entry of `FieldInfo.metadata`
#: rather than by matching the classes that hold them, so `annotated_types`'
#: `Gt` / `MinLen` / `MultipleOf`, `StringConstraints` and pydantic's own
#: general-metadata carrier (which is where `pattern` lands) are all covered by
#: one walk and a class added upstream needs no change here.
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

#: Bounds a `Field` can declare that a marker has nowhere to put. pydantic
#: carries these on its own `_PydanticGeneralMetadata` rather than as
#: `annotated_types` classes, so they are invisible to the fold - and a declared
#: bound disappearing without a word is the defect the fold exists to stop, so
#: the author is told rather than left to find out from a 200.
_UNCARRIED_FIELD_CONSTRAINTS = (
    "max_digits",
    "decimal_places",
    "allow_inf_nan",
)


#: Types with no mutable state at all, so one instance is safe to hand every
#: request. Matched by exact type, because a `str` or `tuple` subclass can hold
#: instance state that its base cannot.
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
        # No instance dictionary, refuses attributes, cached per key, and a deep
        # copy of one returns it - so a copy cannot isolate a zone either.
        zoneinfo.ZoneInfo,
    }
)

#: How deep `is_immutable` looks before answering "cannot vouch for it". A
#: structure deeper than this is pathological, and reaching the bound costs a
#: copy rather than correctness - where recursing without one cost a
#: `RecursionError` at registration.
_MAX_IMMUTABLE_DEPTH = 20


#: Stands in for an attribute whose read raised. Nothing vouches for it, and a
#: bare object is judged mutable, so it fails the immutability walk by itself.
_UNREADABLE = object()


def _slot_descriptors(kind: type[Any]) -> list[types.MemberDescriptorType]:
    """Every slot descriptor in `kind`'s ancestry.

    The descriptors themselves, not names read out of `__slots__`. A private
    name is stored mangled - `__hidden` declared in `Base` is `_Base__hidden` -
    so the declared name reads nothing at all, and the descriptor is also the
    only way to reach a slot a subclass has shadowed with a class attribute of
    the same name.
    """
    return [
        attribute
        for klass in kind.__mro__
        for attribute in vars(klass).values()
        if isinstance(attribute, types.MemberDescriptorType)
    ]


def _guarded(read: Callable[[], Any]) -> Any:
    """Perform `read`; `None` when the attribute is absent, a sentinel when it raises.

    A property or a `__getattr__` can raise anything at all, and a check that
    exists to keep requests safe must not be the thing that stops a route being
    registered. An attribute that cannot be read is not vouched for.
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

    `frozen` stops a field being rebound; it does not stop a list in one being
    appended to, nor `__post_init__` writing through `object.__setattr__`, nor
    a `cached_property` writing straight into the instance dictionary. A class
    that caches into its instance is refused outright, because what it will
    cache has not been written yet.

    Everything reachable is read, not one source of it: the declared fields,
    the instance dictionary when there is one, and every slot in the ancestry.
    Reading only one left three ways to hide a list - a field inherited from a
    slotted base (which an unslotted child's dictionary does not mention), a
    non-field slot, and a slotted base under a dictionary-carrying child.
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
    # Each slot through its own descriptor: a subclass class attribute of the
    # same name shadows a base's slot for `getattr`, which then answers with
    # the class attribute while the slot holds the list that would be shared.
    reachable.extend(
        _guarded(functools.partial(descriptor.__get__, value, kind))
        for descriptor in _slot_descriptors(kind)
    )
    # Guarded as well: a `__getattr__` is consulted for `__dict__` too when the
    # instance has none.
    state = _guarded(functools.partial(getattr, value, "__dict__"))
    if state is _UNREADABLE:
        return False
    if state is not None:
        reachable.extend(state.values())
    return all(is_immutable(member, depth + 1) for member in reachable)


def is_immutable(value: Any, depth: int = 0) -> bool:
    """True when nothing reachable through `value` can be mutated.

    Hashability is not this question: a plain `object()` hashes and still
    carries mutable attributes, and `isinstance((["a"],), Hashable)` is `True`
    though hashing it raises. Neither is `isinstance` alone: a `str`, `tuple`
    or `Path` subclass can hold instance state that its base cannot, so an
    atomic type has to match exactly.

    A value this cannot vouch for is copied per request, which is slower and
    still correct, so every uncertain answer here is `False`. The one widening
    is the `Enum` member a deep copy hands straight back: a copy cannot isolate
    one, so sharing is what happens either way.
    """
    kind = type(value)
    if kind in _ATOMIC_TYPES:
        return True
    if isinstance(value, enum.Enum):
        return True
    if depth >= _MAX_IMMUTABLE_DEPTH:
        return False
    if kind is datetime.datetime or kind is datetime.time:
        # An aware value reaches its `tzinfo`, which is an ordinary object and
        # need not be immutable.
        return is_immutable(value.tzinfo, depth + 1)
    if kind is tuple or kind is frozenset:
        return all(is_immutable(member, depth + 1) for member in value)
    if getattr(_guarded(functools.partial(getattr, kind, "__dataclass_params__")), "frozen", False):
        try:
            return _frozen_dataclass_is_immutable(value, kind, depth)
        except Exception:
            # Reading an arbitrary object is the one thing here that can raise,
            # and a check that exists to keep requests safe must never be the
            # reason a route cannot be registered. Unreadable is not vouched for.
            return False
    return False


def needs_per_request_copy(value: Any) -> bool:
    """True when handing `value` to every request would share mutable state.

    Containers only, matched by `isinstance` because a `list` subclass is a
    mutable container too. A default that is some other mutable object - a
    `datetime`, a client, a model instance - is handed out as written: a
    container is what a handler mutates in place, and copying the rest is
    rarely what the author meant.
    """
    if isinstance(value, (list, dict, set)):
        return True
    if isinstance(value, (tuple, frozenset)):
        return not is_immutable(value)
    return False


#: Frames of stack held in reserve while the trial copy runs. Registration has
#: a shallower stack than a request does, so a structure that copies here could
#: still exhaust the stack there - a 500 on every request that omits the value.
#: Running the trial this much deeper means passing it answers for both.
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

    A member holding a lock, a socket or a database handle cannot be, and
    raising from the factory would turn that into a 500 on every request that
    omits the parameter. Asked once at registration; the value is shared as
    written when the answer is no.
    """
    try:
        margin = _frames_available(_COPY_STACK_MARGIN)
    except RecursionError:
        # Registration is itself running near the limit, so there is no margin
        # to hold back. Refusing the copy for that reason would share an
        # ordinary mutable default rather than copy it, which is the worse
        # answer of the two - the structure's own depth is not the problem here.
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
    # An `AliasChoices` / `AliasPath` names a lookup a marker has one string to
    # express, so only a plain string survives the fold. Reporting it here is
    # what keeps the alias from being dropped in silence, which is the defect
    # the fold exists to stop.
    validation_alias = getattr(field_info, "validation_alias", None)
    if validation_alias is not None and not isinstance(validation_alias, str):
        uncarried.append("validation_alias")
    return tuple(uncarried)


def field_info_declares_constraints(field_info: Any) -> bool:
    """Whether a `Field` declares a bound, rather than only documentation.

    `Field(description=...)` is a legitimate documentation-only spelling, and a
    binding that cannot carry a marker drops nothing enforceable from it - so
    the diagnostics that say "constraints are ignored" must not fire on it.

    Answers False for anything that is not a `FieldInfo`, so a caller can run it
    over an annotation's whole metadata tuple without pre-filtering.
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
        # A static `default` and a `default_factory` are mutually exclusive:
        # the factory exists precisely to build a fresh per-request value, so
        # pinning it to a fixed default at the same time is always a mistake.
        if default is not ... and default_factory is not None:
            raise ValueError("default and default_factory are mutually exclusive")
        self.default = default
        # When set, `default_factory` is invoked on every missing-value resolve
        # so each request receives an independent object. A static mutable
        # default is that same aliasing hazard written the other way: one list
        # on one marker, handed out by identity, so an in-place mutation by one
        # request is visible to the next. Wrap it in a copying factory here -
        # the one place the interpreter and both compiled resolvers read - so
        # `Query(default=[])` is as safe as the bare `tag: list[str] = []` the
        # plan builder already guards. `self.default` keeps the declared value
        # so the published schema still shows it.
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
        # JSON Schema draft 2020-12 Sec. 6.2.1 and OpenAPI 3.1 require
        # `multipleOf` to be strictly greater than zero. Reject non-positive
        # values at declaration time so the error surfaces at app startup
        # rather than producing a non-conformant OpenAPI document.
        if multiple_of is not None and multiple_of <= 0:
            raise ValueError("multiple_of must be positive")
        self.multiple_of = multiple_of
        # the renamed `regex` -> `pattern`; accept either,
        # `pattern` wins when both are supplied.
        self.regex = pattern if pattern is not None else regex
        # Compile the pattern once here, at declaration time, so per-request
        # `validate` does no `re` compile-cache lookup. `regex` stays a
        # string for OpenAPI's `pattern` schema field and error messages.
        self._regex_compiled = re.compile(self.regex) if self.regex is not None else None
        self.deprecated = deprecated
        self.examples = examples
        self.embed = embed
        self.convert_underscores = convert_underscores
        # Opt-in: spread a model annotation across this source's keys. Off by
        # default because a bare model annotation already means "one key holding
        # a JSON document", which is existing, documented behaviour.
        self.group = group
        self.include_in_schema = include_in_schema

    @classmethod
    def from_field_info(cls, field_info: FieldInfo, default: Any = ...) -> Self:
        """Build this marker from a pydantic `Field` carried in `Annotated` metadata.

        `Annotated[int, Field(ge=1, le=10)]` is the spelling users bring from
        other frameworks, and it declares exactly what `Query(ge=1, le=10)`
        does. Folding it into a marker at registration is what makes it
        enforced and published rather than dropped: the slot, the OpenAPI
        document and the MCP tool schema all read the marker and none of them
        knows about `FieldInfo`.

        `default` is the parameter's own default, adopted only when the `Field`
        declares none - `Annotated[int, Field(ge=1)] = 5` keeps 5, while
        `Annotated[int, Field(3, ge=1)] = 5` keeps the `Field`'s 3.
        """
        constraints: dict[str, Any] = {}
        for meta in field_info.metadata:
            for name in _FIELD_CONSTRAINTS:
                value = getattr(meta, name, None)
                if value is not None:
                    constraints[name] = value
        pattern = constraints.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            # pydantic accepts a compiled pattern; the marker recompiles it and
            # the published schema needs the source text, not a `Pattern` repr.
            constraints["pattern"] = pattern.pattern
        if field_info.default_factory is not None:
            # pydantic >= 2.10 also accepts a one-argument factory, handed the
            # already-validated data. A marker calls it with no arguments, so
            # accepting one here would mean a `TypeError` on every request that
            # omits the parameter - refused at registration instead, where the
            # author is looking.
            if _factory_wants_an_argument(field_info.default_factory):
                raise TypeError(
                    "Field(default_factory=...) takes the validated data, which a "
                    "parameter marker cannot supply; use a factory that takes no "
                    "arguments"
                )
            # Mutually exclusive on a marker, and the factory is the stronger
            # declaration: a `Field` carrying one has no static default anyway.
            constraints["default_factory"] = field_info.default_factory
        elif not field_info.is_required():
            constraints["default"] = field_info.default
        elif default is not ...:
            constraints["default"] = default
        # `validation_alias` wins where both are set, which is pydantic's own
        # precedence. Only a plain string can name a wire key; an `AliasChoices`
        # or `AliasPath` describes a lookup a parameter marker cannot express,
        # and `field_info_uncarried_constraints` reports it so the declaration
        # is refused rather than dropped.
        wire_alias = field_info.alias
        validation_alias = getattr(field_info, "validation_alias", None)
        if isinstance(validation_alias, str):
            wire_alias = validation_alias
        return cls(
            alias=wire_alias,
            title=field_info.title,
            description=field_info.description,
            examples=list(field_info.examples) if field_info.examples else None,
            # `FieldInfo.deprecated` arrived after the minimum pydantic this
            # package requires, and it may be a string explaining the
            # replacement rather than a flag.
            deprecated=bool(getattr(field_info, "deprecated", None)),
            **constraints,
        )

    @property
    def has_default(self) -> bool:
        return self.default is not ... or self.default_factory is not None

    def with_default(self, default: Any) -> Self:
        """Return a copy of this marker carrying `default` as its own.

        `Annotated[int, Query()] = 10` declares the default beside the marker
        rather than inside it, and the marker is what the resolver asks for a
        default and what both lowerings publish. A copy, because one marker can
        be shared between parameters - `SHARED = Query()` used twice must not
        take the first parameter's default for the second.
        """
        folded = copy.copy(self)
        folded.default = default
        # The same guard `__init__` applies to a declared default: one mutable
        # value handed to every request is the aliasing hazard, and a copying
        # factory is what `resolve_default` reads.
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
            # `search`, not `fullmatch`: this pattern is published verbatim as
            # the schema's `pattern`, which JSON Schema defines as a partial
            # match, and pydantic reads it the same way. Anchoring it here
            # enforced more than the document advertised, so a value any client
            # generated from that schema treated as valid was refused. Anchor
            # deliberately with `^...$` when a full match is what you mean.
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


#: Where the constraint vocabulary pydantic understands is defined.
#: `annotated_types` holds `Gt`, `Le`, `Len` and the rest; `pydantic` adds a few
#: of its own such as `StringConstraints`.
_CONSTRAINT_MODULES = frozenset({"annotated_types", "pydantic"})


def _is_constraint(meta: Any) -> bool:
    """True when pydantic reads this metadata object as a constraint.

    Identified by where its class is defined rather than by importing the
    constraint classes, so no transitive package becomes a direct dependency of
    this module. A marker of this framework's, a `Doc`, or a bare string
    annotation is defined elsewhere and stays ignored.
    """
    return type(meta).__module__.split(".", 1)[0] in _CONSTRAINT_MODULES


def find_field_info(annotation: Any) -> FieldInfo | None:
    """Return the pydantic `FieldInfo` an annotation declares, or `None`.

    Matched on the `FieldInfo` type rather than on the name the author typed,
    so a renamed import (`from pydantic import Field as F`) is recognised like
    any other.

    Several are merged rather than first-wins. `Annotated` accumulates metadata,
    so one parameter's constraints can arrive split across entries - a shared
    type alias carrying `Field(ge=1)` and the parameter narrowing it with
    `Field(le=10)`. Reading only the first dropped the rest from validation and
    from the published schema at once, which is the worse half: the document
    advertised a bound nothing enforced. Merged through pydantic's own rule, so
    a key's last writer wins here exactly as it does inside a model. Runs at
    registration, not per request.
    """
    metadata = getattr(annotation, "__metadata__", ())
    if not metadata:
        return None
    found = [meta for meta in metadata if isinstance(meta, FieldInfo)]
    bare = [meta for meta in metadata if not isinstance(meta, FieldInfo) and _is_constraint(meta)]
    if bare:
        # `Annotated[int, Gt(0)]` declares what `Field(gt=0)` declares - the
        # keyword is sugar that builds exactly this object - so a constraint
        # written without the wrapper has to be read too; it was dropped from
        # validation and from the published schema alike. Rebuilt through
        # pydantic over an annotation carrying only the constraints, because
        # handing it the original would read this framework's own markers as
        # metadata. Placed first so an explicit `Field()` beside it still wins.
        declared = get_args(annotation)[0]
        rebuilt = cast("type[Any]", Annotated[(declared, *bare)])
        found.insert(0, FieldInfo.from_annotation(rebuilt))
    if not found:
        return None
    return found[0] if len(found) == 1 else FieldInfo.merge_field_infos(*found)
