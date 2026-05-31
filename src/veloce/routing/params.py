"""Parameter markers — declarative bindings for query, path, body, and header values.

Usage:
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
from decimal import Decimal
from typing import Any


class ParamBase:
    """Base class for parameter markers."""

    __slots__ = (
        "default",
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
        "include_in_schema",
    )

    def __init__(
        self,
        default: Any = ...,
        alias: str | None = None,
        title: str | None = None,
        description: str | None = None,
        ge: float | None = None,
        le: float | None = None,
        gt: float | None = None,
        lt: float | None = None,
        min_length: int | None = None,
        max_length: int | None = None,
        multiple_of: float | None = None,
        regex: str | None = None,
        pattern: str | None = None,
        deprecated: bool = False,
        examples: list[Any] | None = None,
        embed: bool = False,
        convert_underscores: bool = True,
        include_in_schema: bool = True,
    ) -> None:
        self.default = default
        self.alias = alias
        self.title = title
        self.description = description
        self.ge = ge
        self.le = le
        self.gt = gt
        self.lt = lt
        self.min_length = min_length
        self.max_length = max_length
        # JSON Schema draft 2020-12 §6.2.1 and OpenAPI 3.1 require
        # `multipleOf` to be strictly greater than zero. Reject non-positive
        # values at declaration time so the error surfaces at app startup
        # rather than producing a non-conformant OpenAPI document.
        if multiple_of is not None and multiple_of <= 0:
            raise ValueError("multiple_of must be positive")
        self.multiple_of = multiple_of
        # the renamed `regex` → `pattern`; accept either,
        # `pattern` wins when both are supplied.
        self.regex = pattern if pattern is not None else regex
        # Compile the pattern once here, at declaration time, so per-request
        # `validate` does no `re` compile-cache lookup. `regex` stays a
        # string for OpenAPI's `pattern` schema field and error messages.
        self._regex_compiled = re.compile(self.regex) if self.regex is not None else None
        self.deprecated = deprecated
        self.examples = examples
        # `Body(embed=True)` — nest the value under the param
        # name inside the JSON body instead of treating the whole body
        # as the value. Only meaningful for `Body` markers.
        self.embed = embed
        # `Header(convert_underscores=...)` — when True (default)
        # an un-aliased `Header` param's name has `_` rewritten to `-`
        # (`x_token` → `x-token`). Only meaningful for `Header` markers.
        self.convert_underscores = convert_underscores
        # `include_in_schema` — when False the parameter is
        # still resolved at runtime but omitted from the OpenAPI
        # `parameters` list.
        self.include_in_schema = include_in_schema

    @property
    def has_default(self) -> bool:
        return self.default is not ...

    def validate(self, value: Any, name: str) -> Any:
        """Validate value against constraints."""
        if value is None and self.has_default:
            return self.default

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
