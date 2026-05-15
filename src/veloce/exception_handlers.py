"""Default exception handlers exposed as importable functions.

Veloce exposes its built-in handlers here so applications can wrap or
delegate to them: `http_exception_handler` renders an `HTTPException`
as JSON; `request_validation_exception_handler` renders a
`RequestValidationError` as a 422 with the structured error list.

These produce the same responses the dispatcher's inline error path
does — they exist for callers that register custom handlers but want
to fall back to the default rendering.
"""

from __future__ import annotations

from typing import Any

from veloce.exceptions import HTTPException, RequestValidationError
from veloce.http.response import JSONResponse, Response


async def http_exception_handler(request: Any, exc: HTTPException) -> Response:
    """Render an `HTTPException` as a JSON `{"detail": ...}` response.

    Honours `exc.status_code`, `exc.detail` (falling back to the
    subclass description), and `exc.headers`.
    """
    status = exc.status_code or 500
    detail = exc.detail or getattr(exc, "description", "") or "Error"
    return JSONResponse(
        {"detail": detail},
        status_code=status,
        headers=dict(exc.headers) if getattr(exc, "headers", None) else None,
    )


async def request_validation_exception_handler(
    request: Any, exc: RequestValidationError
) -> Response:
    """Render a `RequestValidationError` as a 422 with the error list.

    uses the structured shape `{"detail": [ ...per-field errors... ]}`.
    """
    return JSONResponse({"detail": exc.errors or []}, status_code=422)
