---
description: Veloce API reference - exceptions & status codes.
---

# Exceptions & Status Codes

The exception hierarchy, the built-in handlers, and the status-code constants.

Every exception below inherits `VeloceError`, so `except VeloceError` catches
anything the framework raised regardless of which family it came from.

::: veloce.VeloceError
::: veloce.HTTPException
::: veloce.ValidationError
::: veloce.RequestValidationError
::: veloce.WebSocketException
::: veloce.WebSocketDisconnect
::: veloce.WebSocketRequestValidationError
::: veloce.BuildError
::: veloce.ConfigurationError
::: veloce.DuplicateRouteError
::: veloce.FilesKeyError
::: veloce.SetupError
::: veloce.http_exception_handler
::: veloce.request_validation_exception_handler
::: veloce.status

## Named HTTP errors

One class per standard status code. Each carries a fixed `code` and
`description`, so `raise NotFound("no such item")` produces a `404` whose body
defaults to `"Not Found"`. `abort(404)` raises the same class, which is why a
handler registered against `NotFound` matches an `abort()` too.

`ServerNotImplemented` is the 501 class: `NotImplemented` is a Python builtin,
so the obvious name is unavailable.

::: veloce.BadRequest
::: veloce.Unauthorized
::: veloce.PaymentRequired
::: veloce.Forbidden
::: veloce.NotFound
::: veloce.MethodNotAllowed
::: veloce.NotAcceptable
::: veloce.ProxyAuthenticationRequired
::: veloce.RequestTimeout
::: veloce.Conflict
::: veloce.Gone
::: veloce.LengthRequired
::: veloce.PreconditionFailed
::: veloce.RequestEntityTooLarge
::: veloce.RequestURITooLong
::: veloce.UnsupportedMediaType
::: veloce.RangeNotSatisfiable
::: veloce.ExpectationFailed
::: veloce.ImATeapot
::: veloce.UnprocessableEntity
::: veloce.TooManyRequests
::: veloce.InternalServerError
::: veloce.ServerNotImplemented
::: veloce.BadGateway
::: veloce.ServiceUnavailable
::: veloce.GatewayTimeout

## Warnings

::: veloce.VeloceDeprecationWarning
