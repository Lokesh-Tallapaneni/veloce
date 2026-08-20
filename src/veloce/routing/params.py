"""Parameter markers — declarative bindings for query, path, body, and header values.

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

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any

from typing_extensions import Doc


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
            Doc(
                "Deprecated alias for `pattern`; the regular expression the value must fully match."
            ),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc("Regular expression the value must fully match; takes precedence over `regex`."),
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
        # so each request receives an independent object, preventing the
        # shared-mutable aliasing a static `Query(default=[])` would cause.
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
        # `Body(embed=True)` - nest the value under the param
        # name inside the JSON body instead of treating the whole body
        # as the value. Only meaningful for `Body` markers.
        self.embed = embed
        # `Header(convert_underscores=...)` - when True (default)
        # an un-aliased `Header` param's name has `_` rewritten to `-`
        # (`x_token` -> `x-token`). Only meaningful for `Header` markers.
        self.convert_underscores = convert_underscores
        # Opt-in: spread a model annotation across this source's keys. Off by
        # default because a bare model annotation already means "one key holding
        # a JSON document", which is existing, documented behaviour.
        self.group = group
        # `include_in_schema` - when False the parameter is
        # still resolved at runtime but omitted from the OpenAPI
        # `parameters` list.
        self.include_in_schema = include_in_schema

    @property
    def has_default(self) -> bool:
        return self.default is not ... or self.default_factory is not None

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
            if self._regex_compiled is not None and not self._regex_compiled.fullmatch(value):
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
