"""Parameter declaration classes.

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

from typing import Any


class _ParamBase:
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
        # JSON Schema `multipleOf` — the value must be an exact multiple
        # of this number.
        self.multiple_of = multiple_of
        # the renamed `regex` → `pattern`; accept either,
        # `pattern` wins when both are supplied.
        self.regex = pattern if pattern is not None else regex
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

        if isinstance(value, (int, float)):
            if self.ge is not None and value < self.ge:
                raise ValueError(f"{name} must be >= {self.ge}")
            if self.le is not None and value > self.le:
                raise ValueError(f"{name} must be <= {self.le}")
            if self.gt is not None and value <= self.gt:
                raise ValueError(f"{name} must be > {self.gt}")
            if self.lt is not None and value >= self.lt:
                raise ValueError(f"{name} must be < {self.lt}")
            if self.multiple_of is not None:
                quotient = value / self.multiple_of
                if abs(quotient - round(quotient)) > 1e-9:
                    raise ValueError(f"{name} must be a multiple of {self.multiple_of}")

        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise ValueError(f"{name} must have at least {self.min_length} characters")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"{name} must have at most {self.max_length} characters")
            if self.regex is not None:
                import re

                if not re.match(self.regex, value):
                    raise ValueError(f"{name} does not match pattern {self.regex}")

        return value


class Query(_ParamBase):
    """Query parameter declaration."""

    pass


class Path(_ParamBase):
    """Path parameter declaration."""

    pass


class Body(_ParamBase):
    """Request body parameter declaration."""

    pass


class Form(_ParamBase):
    """Form field parameter declaration."""

    pass


class File(_ParamBase):
    """File upload parameter declaration."""

    pass


class Header(_ParamBase):
    """HTTP header parameter declaration."""

    pass


class Cookie(_ParamBase):
    """Cookie parameter declaration."""

    pass
