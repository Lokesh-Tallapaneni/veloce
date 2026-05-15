"""HTTP and validation exceptions.

Each named HTTP exception below corresponds to a status code from RFC 9110
(HTTP Semantics) and RFC 6585 (Additional HTTP Status Codes). The subclass
identity lets handlers register a single class and catch every subclass via
the standard Python exception-class hierarchy.
"""

from __future__ import annotations

from typing import Any, ClassVar


class HTTPException(Exception):
    """HTTP error with status code and detail.

    Either subclass with a fixed `code` (and optional `description`),
    or instantiate `HTTPException(status_code, detail, headers)` directly.
    """

    # Default code/description supplied by subclasses. The base class is
    # spec-agnostic; callers passing `status_code` explicitly always win.
    code: ClassVar[int | None] = None
    description: ClassVar[str] = ""

    def __init__(
        self,
        status_code: int | str | None = None,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        # Subclass-style call shorthand: `NotFound("user gone")` passes a
        # string in the first position. Treat it as `detail` and fall back
        # to the subclass's `code`. Keeps the short call form
        # while preserving the explicit `HTTPException(404, ...)` form.
        if isinstance(status_code, str):
            if detail:
                raise TypeError(
                    "HTTPException: passed `detail` twice — either as the "
                    "first positional argument or as `detail=`, not both"
                )
            detail = status_code
            status_code = None

        # `code` from the subclass acts as the default; explicit
        # `status_code=` argument overrides.
        if status_code is None:
            if self.code is None:
                raise TypeError(
                    "HTTPException requires a status_code unless used via a "
                    "subclass that defines `code`"
                )
            status_code = self.code
        self.status_code = status_code
        self.detail = detail or self.description
        self.headers = headers or {}
        super().__init__(self.detail)


# ── 4xx — Client errors (RFC 9110 §15.5) ─────────────────────────────


class BadRequest(HTTPException):
    code = 400
    description = "Bad Request"


class Unauthorized(HTTPException):
    code = 401
    description = "Unauthorized"


class PaymentRequired(HTTPException):
    code = 402
    description = "Payment Required"


class Forbidden(HTTPException):
    code = 403
    description = "Forbidden"


class NotFound(HTTPException):
    code = 404
    description = "Not Found"


class Aborter:
    """A callable that turns a status code into an HTTPException.

    Used as `app.aborter(404)` or `app.aborter(403, "Forbidden")`.
    The mapping from status code to exception class is configurable
    by subclass — override `mapping` to add custom exception classes.
    Identical to the module-level `abort()` helper in behaviour, but
    expressed as an instance so subclasses can override the mapping
    without monkey-patching the helper.
    """

    mapping: dict[int, type] = {}  # populated below after subclass defs

    def __init__(self, extra_mapping: dict[int, type] | None = None) -> None:
        # Per-instance overlay on top of the class-level mapping.
        self._mapping: dict[int, type] = {}
        if extra_mapping:
            self._mapping.update(extra_mapping)

    def __call__(
        self,
        code: int,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        if not detail:
            from http import HTTPStatus

            try:
                detail = HTTPStatus(code).phrase
            except ValueError:
                detail = "Error"
        cls = self._mapping.get(code) or self.mapping.get(code) or exception_for_status(code)
        raise cls(status_code=code, detail=detail, headers=headers)


class MethodNotAllowed(HTTPException):
    code = 405
    description = "Method Not Allowed"


class NotAcceptable(HTTPException):
    code = 406
    description = "Not Acceptable"


class ProxyAuthenticationRequired(HTTPException):
    code = 407
    description = "Proxy Authentication Required"


class RequestTimeout(HTTPException):
    code = 408
    description = "Request Timeout"


class Conflict(HTTPException):
    code = 409
    description = "Conflict"


class Gone(HTTPException):
    code = 410
    description = "Gone"


class LengthRequired(HTTPException):
    code = 411
    description = "Length Required"


class PreconditionFailed(HTTPException):
    code = 412
    description = "Precondition Failed"


class RequestEntityTooLarge(HTTPException):
    code = 413
    description = "Content Too Large"


class RequestURITooLong(HTTPException):
    code = 414
    description = "URI Too Long"


class UnsupportedMediaType(HTTPException):
    code = 415
    description = "Unsupported Media Type"


class RangeNotSatisfiable(HTTPException):
    code = 416
    description = "Range Not Satisfiable"


class ExpectationFailed(HTTPException):
    code = 417
    description = "Expectation Failed"


class ImATeapot(HTTPException):
    # RFC 2324 — not technically RFC 9110 but ubiquitous in test suites.
    code = 418
    description = "I'm a teapot"


class UnprocessableEntity(HTTPException):
    # Reused by RFC 9110 §15.5.21 (formerly WebDAV-only); also the typed-DI 422.
    code = 422
    description = "Unprocessable Content"


class TooManyRequests(HTTPException):
    # RFC 6585 §4.
    code = 429
    description = "Too Many Requests"


# ── 5xx — Server errors (RFC 9110 §15.6) ─────────────────────────────


class InternalServerError(HTTPException):
    code = 500
    description = "Internal Server Error"


class NotImplemented_(HTTPException):
    # Trailing underscore — `NotImplemented` is a builtin singleton.
    code = 501
    description = "Not Implemented"


class BadGateway(HTTPException):
    code = 502
    description = "Bad Gateway"


class ServiceUnavailable(HTTPException):
    code = 503
    description = "Service Unavailable"


class GatewayTimeout(HTTPException):
    code = 504
    description = "Gateway Timeout"


# ── Validation ───────────────────────────────────────────────────────


class ValidationError(UnprocessableEntity):
    """Request validation error (422).

    Subclasses `UnprocessableEntity` so handlers registered against either
    `UnprocessableEntity` or `HTTPException` catch it via the MRO walk
    Veloce performs in error dispatch.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(detail=str(errors))


class RequestValidationError(ValidationError):
    """Framework-level request validation failure (422).

    Raised by the dependency resolver when path / query / header / cookie /
    body / form / file parameters fail validation. Distinct from a
    user-level `ValidationError` so handlers can pick one or the other:

        @app.exception_handler(RequestValidationError)
        async def on_req_invalid(request, exc):
            return JSONResponse({"errors": exc.errors}, status_code=422)

    Subclasses `ValidationError` so existing `except ValidationError`
    handlers continue to catch it via the MRO walk.
    """

    pass


class WebSocketDisconnect(Exception):
    """WebSocket connection closed."""

    def __init__(self, code: int = 1000) -> None:
        self.code = code


class WebSocketException(Exception):
    """Raised inside a WebSocket handler to close the connection cleanly.

    ASGI shape. The dispatch layer catches it and sends a
    close frame carrying `code` (RFC 6455 §7.4.1) and the optional
    `reason` — no traceback is propagated, since this is an
    application-driven close rather than an internal error.
    """

    def __init__(self, code: int, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason or ''}")


class WebSocketRequestValidationError(RequestValidationError):
    """A WebSocket dependency failed parameter validation.

    Raised when a `Depends()` resolved during a
    WebSocket handshake reports a `RequestValidationError`. The
    dispatch layer closes the connection with code 1008 (policy
    violation) rather than 1011 (internal error), since the failure is
    a client-side contract violation, not a server fault.
    """

    pass


class BuildError(LookupError):
    """`url_for` could not build a URL for the given endpoint.

    Carries the endpoint name and the values that were being substituted
    so registered `app.url_build_error_handlers` callbacks can recover
    (e.g. fall back to a different endpoint, or fetch from an external
    routing table) by inspecting the failure and returning a URL string.
    """

    def __init__(self, endpoint: str, values: dict[str, Any], method: str | None = None) -> None:
        self.endpoint = endpoint
        self.values = values
        self.method = method
        super().__init__(f"Could not build URL for endpoint {endpoint!r}")


# ── Lookup table — status code → subclass ────────────────────────────

_BY_CODE: dict[int, type[HTTPException]] = {
    400: BadRequest,
    401: Unauthorized,
    402: PaymentRequired,
    403: Forbidden,
    404: NotFound,
    405: MethodNotAllowed,
    406: NotAcceptable,
    407: ProxyAuthenticationRequired,
    408: RequestTimeout,
    409: Conflict,
    410: Gone,
    411: LengthRequired,
    412: PreconditionFailed,
    413: RequestEntityTooLarge,
    414: RequestURITooLong,
    415: UnsupportedMediaType,
    416: RangeNotSatisfiable,
    417: ExpectationFailed,
    418: ImATeapot,
    422: UnprocessableEntity,
    429: TooManyRequests,
    500: InternalServerError,
    501: NotImplemented_,
    502: BadGateway,
    503: ServiceUnavailable,
    504: GatewayTimeout,
}


def exception_for_status(status_code: int) -> type[HTTPException]:
    """Return the registered subclass for a status code, or the base class.

    Used by `abort()` to raise a specifically-typed exception so handlers
    registered against the subclass match.
    """
    return _BY_CODE.get(status_code, HTTPException)
