"""HTTP and validation exceptions — plus default exception handlers.

Each named HTTP exception below corresponds to a status code from RFC 9110
(HTTP Semantics) and RFC 6585 (Additional HTTP Status Codes). The subclass
identity lets handlers register a single class and catch every subclass via
the standard Python exception-class hierarchy.

The two default handler functions at the end of this module render
``HTTPException`` and ``RequestValidationError`` as JSON responses.
Applications can wrap or delegate to them when registering custom handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from veloce.http.response import JSONResponse
from veloce.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
    HTTP_406_NOT_ACCEPTABLE,
    HTTP_407_PROXY_AUTHENTICATION_REQUIRED,
    HTTP_408_REQUEST_TIMEOUT,
    HTTP_409_CONFLICT,
    HTTP_410_GONE,
    HTTP_411_LENGTH_REQUIRED,
    HTTP_412_PRECONDITION_FAILED,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_414_REQUEST_URI_TOO_LONG,
    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
    HTTP_417_EXPECTATION_FAILED,
    HTTP_418_IM_A_TEAPOT,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
    WS_1000_NORMAL_CLOSURE,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.response import Response


def _error_handler_key_error(key: Any) -> str:
    """Build the message for a handler key that is neither a status nor a class.

    Lives here rather than beside either registration site so the app-level and
    blueprint-level checks cannot drift: the blueprint one was missing entirely,
    and a non-class key sat in an MRO-matched table where it could never fire.
    """
    lead = "error handler keys must be an int status code or an exception class"
    if isinstance(key, str):
        if key.isdigit():
            return f"{lead}; got the string {key!r}. Write {int(key)} without the quotes."
        return f"{lead}; got the string {key!r}. Pass the class itself, not its name."
    return f"{lead}; got {key!r}."


class VeloceError(Exception):
    """Root of every exception Veloce raises.

    Mixed into each exception family the framework defines - HTTP errors,
    validation failures, WebSocket closes, routing and setup errors, JWT and
    signature failures - so `except VeloceError` answers "did this come from
    Veloce?" in one clause. Families that already subclassed a stdlib type
    keep it: `DuplicateRouteError` is still a `ValueError` and
    `FilesKeyError` is still a `KeyError`, so every handler that matched
    before still matches. `VeloceError` is listed first in those bases, so a
    handler registered against it wins the MRO walk over a broader stdlib
    handler.
    """


class HTTPException(VeloceError):
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
                    "HTTPException: passed `detail` twice - either as the "
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
        # Copy on the way in. This mapping becomes the response's `headers`,
        # which response middleware (CORS / Session / SecurityHeaders) mutates
        # in place, so sharing it with the raiser lets one request's `Set-Cookie`
        # or `Access-Control-Allow-Origin` accumulate on a caller-held dict and
        # ship on every later raise. A security scheme that caches its
        # `WWW-Authenticate` challenge once - the sensible thing to do, since it
        # is request-invariant - is exactly the shape that leaks. Copying here
        # rather than at each raise site means no scheme, present or future, has
        # to know the rule. The cost lands only on a request that has already
        # failed and is about to serialise an error body.
        self.headers = dict(headers) if headers else {}
        super().__init__(self.detail)


# ── 4xx - Client errors (RFC 9110 Sec. 15.5) ──────────────


class BadRequest(HTTPException):
    code = HTTP_400_BAD_REQUEST
    description = "Bad Request"


class Unauthorized(HTTPException):
    code = HTTP_401_UNAUTHORIZED
    description = "Unauthorized"


class PaymentRequired(HTTPException):
    code = HTTP_402_PAYMENT_REQUIRED
    description = "Payment Required"


class Forbidden(HTTPException):
    code = HTTP_403_FORBIDDEN
    description = "Forbidden"


class NotFound(HTTPException):
    code = HTTP_404_NOT_FOUND
    description = "Not Found"


class MethodNotAllowed(HTTPException):
    code = HTTP_405_METHOD_NOT_ALLOWED
    description = "Method Not Allowed"


class NotAcceptable(HTTPException):
    code = HTTP_406_NOT_ACCEPTABLE
    description = "Not Acceptable"


class ProxyAuthenticationRequired(HTTPException):
    code = HTTP_407_PROXY_AUTHENTICATION_REQUIRED
    description = "Proxy Authentication Required"


class RequestTimeout(HTTPException):
    code = HTTP_408_REQUEST_TIMEOUT
    description = "Request Timeout"


class Conflict(HTTPException):
    code = HTTP_409_CONFLICT
    description = "Conflict"


class Gone(HTTPException):
    code = HTTP_410_GONE
    description = "Gone"


class LengthRequired(HTTPException):
    code = HTTP_411_LENGTH_REQUIRED
    description = "Length Required"


class PreconditionFailed(HTTPException):
    code = HTTP_412_PRECONDITION_FAILED
    description = "Precondition Failed"


class RequestEntityTooLarge(HTTPException):
    code = HTTP_413_REQUEST_ENTITY_TOO_LARGE
    description = "Content Too Large"

    #: The `MAX_CONTENT_LENGTH` the body exceeded, when the raiser knows it.
    #: Rendered into the error body so a streamed body's refusal describes
    #: itself the same way the eager refusal paths do; `None` when a caller
    #: raises this directly without a limit to report.
    limit: int | None = None


class RequestURITooLong(HTTPException):
    code = HTTP_414_REQUEST_URI_TOO_LONG
    description = "URI Too Long"


class UnsupportedMediaType(HTTPException):
    code = HTTP_415_UNSUPPORTED_MEDIA_TYPE
    description = "Unsupported Media Type"


class RangeNotSatisfiable(HTTPException):
    code = HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
    description = "Range Not Satisfiable"


class ExpectationFailed(HTTPException):
    code = HTTP_417_EXPECTATION_FAILED
    description = "Expectation Failed"


class ImATeapot(HTTPException):
    # RFC 2324 - not technically RFC 9110 but ubiquitous in test suites.
    code = HTTP_418_IM_A_TEAPOT
    description = "I'm a teapot"


class UnprocessableEntity(HTTPException):
    # Reused by RFC 9110 Sec. 15.5.21 (formerly WebDAV-only); also the typed-DI 422.
    code = HTTP_422_UNPROCESSABLE_ENTITY
    description = "Unprocessable Content"


class TooManyRequests(HTTPException):
    # RFC 6585 Sec. 4.
    code = HTTP_429_TOO_MANY_REQUESTS
    description = "Too Many Requests"


# ── 5xx - Server errors (RFC 9110 Sec. 15.6) ──────────────


class InternalServerError(HTTPException):
    code = HTTP_500_INTERNAL_SERVER_ERROR
    description = "Internal Server Error"


class ServerNotImplemented(HTTPException):
    # `NotImplemented` is a builtin singleton, so the 501 class cannot carry the
    # status phrase as its name. `ServerNotImplemented` reads alongside
    # `InternalServerError` and `ServiceUnavailable` in the 5xx block, and is the
    # name a traceback shows.
    code = HTTP_501_NOT_IMPLEMENTED
    description = "Not Implemented"


# The spelling this class carried before it was exported. Kept bound to the same
# object so an existing `from veloce.exceptions import NotImplemented_` and every
# `except` clause written against it go on working.
NotImplemented_ = ServerNotImplemented


class BadGateway(HTTPException):
    code = HTTP_502_BAD_GATEWAY
    description = "Bad Gateway"


class ServiceUnavailable(HTTPException):
    code = HTTP_503_SERVICE_UNAVAILABLE
    description = "Service Unavailable"


class GatewayTimeout(HTTPException):
    code = HTTP_504_GATEWAY_TIMEOUT
    description = "Gateway Timeout"


# ── Validation ────────────────────────────────────────────


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
            return JSONResponse(
                {"errors": exc.errors},
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            )

    Subclasses `ValidationError` so existing `except ValidationError`
    handlers continue to catch it via the MRO walk.
    """


# ── WebSocket exceptions ──────────────────────────────────


class WebSocketDisconnect(VeloceError):
    """WebSocket connection closed."""

    def __init__(self, code: int = WS_1000_NORMAL_CLOSURE) -> None:
        # Through `super()`, so the message does not depend on how the caller
        # spelled the call. `BaseException.__new__` populates `args` from
        # *positional* arguments only, so without this
        # `WebSocketDisconnect(1006)` stringified as "1006" while
        # `WebSocketDisconnect(code=1006)` and the default stringified as "" -
        # the same close code logging differently depending on the call site.
        super().__init__(code)
        self.code = code


class WebSocketException(VeloceError):
    """Raised inside a WebSocket handler to close the connection cleanly.

    ASGI shape. The dispatch layer catches it and sends a
    close frame carrying `code` (RFC 6455 Sec. 7.4.1) and the optional
    `reason` - no traceback is propagated, since this is an
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


# ── Other exception families ──────────────────────────────


class FilesKeyError(VeloceError, KeyError):
    """Descriptive miss on ``request.files`` raised in debug mode.

    Subclasses ``KeyError`` so handlers that already catch the bare lookup
    miss keep working, while the message explains the most common cause:
    the field was submitted as a plain form value (missing
    ``enctype="multipart/form-data"``) or the body was JSON rather than a
    multipart upload. Only raised when ``app.debug`` is set; production
    keeps the plain ``KeyError`` semantics.
    """

    def __init__(self, message: str) -> None:
        self._message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self._message


class BuildError(VeloceError, LookupError):
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


class DuplicateRouteError(VeloceError, ValueError):
    """Two handlers were registered for the same path and HTTP method.

    Raised at registration time when a route would silently overwrite an
    existing handler. Carries the conflicting path, method, and both handler
    qualified names so the message points at the exact collision. Configure
    the policy per router with `on_duplicate="error"|"warn"|"override"`.
    """

    def __init__(
        self,
        path: str,
        method: str,
        existing: str,
        incoming: str,
    ) -> None:
        self.path = path
        self.method = method
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"Duplicate route: {method} {path} is already handled by {existing!r}; "
            f"{incoming!r} would overwrite it. Pass on_duplicate='override' to allow "
            "replacement or rename one of the routes."
        )


class SetupError(VeloceError, RuntimeError):
    """A registration ran after the application started serving.

    Routes, hooks, blueprints, middleware, and similar setup must be wired
    before the first request is dispatched. Once serving begins the route
    table and hook lists are frozen, so a late mutation - which would race
    in-flight requests under concurrent ASGI dispatch - raises this instead
    of silently corrupting the live application. The lock is relaxed in
    `DEBUG`/`TESTING` so hot-reload and test monkeypatching stay ergonomic.
    """


class ConfigurationError(VeloceError, RuntimeError):
    """A handler or route was declared in a way that cannot be resolved.

    Raised at registration time, never per request, so a genuinely ambiguous
    parameter declaration becomes a startup error instead of a silent
    mis-binding discovered only at runtime. Carries the offending parameter
    name so the message points straight at the conflict.
    """


# ── Lookup table - status code -> subclass ────────────────

_BY_CODE: dict[int, type[HTTPException]] = {
    HTTP_400_BAD_REQUEST: BadRequest,
    HTTP_401_UNAUTHORIZED: Unauthorized,
    HTTP_402_PAYMENT_REQUIRED: PaymentRequired,
    HTTP_403_FORBIDDEN: Forbidden,
    HTTP_404_NOT_FOUND: NotFound,
    HTTP_405_METHOD_NOT_ALLOWED: MethodNotAllowed,
    HTTP_406_NOT_ACCEPTABLE: NotAcceptable,
    HTTP_407_PROXY_AUTHENTICATION_REQUIRED: ProxyAuthenticationRequired,
    HTTP_408_REQUEST_TIMEOUT: RequestTimeout,
    HTTP_409_CONFLICT: Conflict,
    HTTP_410_GONE: Gone,
    HTTP_411_LENGTH_REQUIRED: LengthRequired,
    HTTP_412_PRECONDITION_FAILED: PreconditionFailed,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE: RequestEntityTooLarge,
    HTTP_414_REQUEST_URI_TOO_LONG: RequestURITooLong,
    HTTP_415_UNSUPPORTED_MEDIA_TYPE: UnsupportedMediaType,
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE: RangeNotSatisfiable,
    HTTP_417_EXPECTATION_FAILED: ExpectationFailed,
    HTTP_418_IM_A_TEAPOT: ImATeapot,
    HTTP_422_UNPROCESSABLE_ENTITY: UnprocessableEntity,
    HTTP_429_TOO_MANY_REQUESTS: TooManyRequests,
    HTTP_500_INTERNAL_SERVER_ERROR: InternalServerError,
    HTTP_501_NOT_IMPLEMENTED: ServerNotImplemented,
    HTTP_502_BAD_GATEWAY: BadGateway,
    HTTP_503_SERVICE_UNAVAILABLE: ServiceUnavailable,
    HTTP_504_GATEWAY_TIMEOUT: GatewayTimeout,
}


def exception_for_status(status_code: int) -> type[HTTPException]:
    """Return the registered subclass for a status code, or the base class.

    Used by `abort()` to raise a specifically-typed exception so handlers
    registered against the subclass match.
    """
    return _BY_CODE.get(status_code, HTTPException)


# ── Default exception handlers ────────────────────────────


def http_exception_payload(exc: Any) -> dict[str, Any]:
    """Build the JSON body for an `HTTPException`, in one place.

    Used by every path that renders one: the request cycle, the out-of-band
    `handle_http_exception`, and the public `http_exception_handler`. They were
    separate copies that disagreed - an exception with an empty detail rendered
    `{"detail": ""}` through a request and `{"detail": "Error"}` elsewhere, and
    only one of them carried a body-limit refusal's `limit`.

    `exc.detail` already falls back to the subclass `description` at
    construction, so no second fallback is applied here. A validation error's
    structured `.errors` list is emitted verbatim.
    """
    structured = getattr(exc, "errors", None)
    payload: dict[str, Any] = {
        "detail": structured if structured is not None else exc.detail,
        "status_code": exc.status_code,
    }
    # A body-limit refusal carries the limit it tripped, so a streamed body's
    # 413 describes itself the same way the eager refusal paths do.
    limit = getattr(exc, "limit", None)
    if limit is not None:
        payload["limit"] = limit
    return payload


async def http_exception_handler(request: Any, exc: HTTPException) -> Response:
    """Render an ``HTTPException`` as a JSON ``{"detail": ..., "status_code": ...}`` response.

    Honours ``exc.status_code``, ``exc.detail`` (falling back to the
    subclass description), and ``exc.headers``.
    """
    payload = http_exception_payload(exc)
    return JSONResponse(
        payload,
        status_code=payload["status_code"],
        headers=dict(exc.headers) if getattr(exc, "headers", None) else None,
    )


async def request_validation_exception_handler(
    request: Any, exc: RequestValidationError
) -> Response:
    """Render a ``RequestValidationError`` as a 422 with the error list.

    Uses the structured shape ``{"detail": [ ...per-field errors... ]}``.
    """
    return JSONResponse({"detail": exc.errors or []}, status_code=HTTP_422_UNPROCESSABLE_ENTITY)


# Backward-compat re-export - Aborter moved to veloce.helpers.
def __getattr__(name: str) -> Any:
    if name == "Aborter":
        from veloce.helpers import Aborter

        return Aborter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
