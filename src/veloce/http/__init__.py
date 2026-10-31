"""HTTP sub-package — request, response, and data structures."""

from __future__ import annotations

from typing import Any

from veloce.http.cache_control import CacheControl
from veloce.http.datastructures import (
    URL,
    AcceptHeader,
    Address,
    Authorization,
    Cookies,
    FormData,
    Headers,
    QueryParams,
    RangeSpec,
    State,
    UploadFile,
)
from veloce.http.formparsers import parse_multipart_form
from veloce.http.header_set import HeaderSet
from veloce.http.request import Request
from veloce.http.response import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    header_get,
    header_key,
    header_pop,
    header_present,
)

__all__ = [
    "header_pop",
    "AcceptHeader",
    "Address",
    "Authorization",
    "CacheControl",
    "Cookies",
    "Request",
    "Response",
    "JSONResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "StreamingResponse",
    "FileResponse",
    "RangeSpec",
    "State",
    "UploadFile",
    "URL",
    "FormData",
    "HeaderSet",
    "Headers",
    "QueryParams",
    "parse_multipart_form",
    "header_key",
    "header_get",
    "header_present",
]


def __getattr__(name: str) -> Any:
    """Resolve a deprecated response alias through the top-level gateway.

    The warning is raised there, once per process, so this path does not add a
    second one.
    """
    if name in {"ORJSONResponse", "UJSONResponse"}:
        import veloce

        globals()[name] = value = getattr(veloce, name)
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
