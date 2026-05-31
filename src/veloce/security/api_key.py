"""API Key authentication schemes — header, query, cookie."""

from __future__ import annotations

from typing import Any

from veloce.http.request import Request
from veloce.security._utils import _extract_api_key


class _APIKeyBase:
    """Shared logic for `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`.

    Each subclass differs only in which `Request` collection it pulls
    the key from. The `__init__` (store `name` + `auto_error`) and the
    delegation to `_extract_api_key` were three copies of the same five
    lines; centralising prevents the three from drifting apart on a
    future change to the extraction signature.
    """

    _source_attr: str = ""  # subclass overrides — Request attribute name
    __slots__ = ("name", "auto_error")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # A slotted base silently grows a per-instance __dict__ if a subclass
        # omits __slots__, undoing the memory win — fail loudly instead.
        if "__slots__" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must define __slots__ (use __slots__ = () if no new fields needed)"
            )
        if not cls._source_attr:
            raise TypeError(f"{cls.__name__} must set _source_attr to a Request attribute name")

    def __init__(self, name: str, auto_error: bool = True) -> None:
        # Keep the user's casing for the OpenAPI spec; header lookup goes
        # through the case-insensitive `Headers` (CIMultiDict) so the case
        # doesn't matter at read time.
        self.name = name
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        source: Any = getattr(request, self._source_attr)
        return _extract_api_key(source, self.name, self.auto_error)


class APIKeyHeader(_APIKeyBase):
    """API Key authentication via HTTP header."""

    __slots__ = ()
    _source_attr = "headers"


class APIKeyQuery(_APIKeyBase):
    """API Key authentication via query parameter."""

    __slots__ = ()
    _source_attr = "query_params"


class APIKeyCookie(_APIKeyBase):
    """API Key authentication via cookie."""

    __slots__ = ()
    _source_attr = "cookies"
