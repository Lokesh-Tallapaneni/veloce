"""ASGI transport — the `__call__` entry, request/websocket dispatch handoff,
and the buffered/streaming response emit.

A mixin on `Veloce`. `__call__` resolves the compiled pipeline and either threads
it into `_asgi_app` (the common no-wrapper case) or runs the ASGI wrapper stack;
`_asgi_app` builds the `Request`, hands HTTP off to `handle_request` and websockets
to `_run_websocket`, and writes the response bytes. The pre-encoded emit helpers
live here too so this module owns the whole emit path.
"""

from __future__ import annotations

import builtins
import contextlib
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from veloce import status
from veloce._constants import (
    MIME_TEXT_PLAIN_UTF8,
    MSG_INVALID_QUERY_STRING,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
    MSG_REQUEST_BODY_EXCEEDS_MAX,
)
from veloce._internal import (
    MIME_HTML,
    MIME_JSON,
    MIME_OCTET,
    _encode_header_value,
    _extract_host,
    _reject_header_crlf,
)
from veloce._pipeline import (
    CompiledPipeline,
    flatten_asgi_wrap,
)
from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_RESPONSE_BODY,
    ASGI_EVENT_HTTP_RESPONSE_START,
    ASGI_EVENT_LIFESPAN_SHUTDOWN,
    ASGI_EVENT_LIFESPAN_SHUTDOWN_COMPLETE,
    ASGI_EVENT_LIFESPAN_SHUTDOWN_FAILED,
    ASGI_EVENT_LIFESPAN_STARTUP,
    ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE,
    ASGI_EVENT_LIFESPAN_STARTUP_FAILED,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_SCOPE_HTTP,
    ASGI_SCOPE_LIFESPAN,
    ASGI_SCOPE_WEBSOCKET,
    HTTP_METHOD_HEAD,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    RAW_HEADER_CONTENT_LENGTH,
    RAW_HEADER_CONTENT_TYPE,
    RAW_HEADER_SET_COOKIE,
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.app.urls import URLRule as URLRule
from veloce.dependency import DependencyResolver
from veloce.exceptions import (
    RequestValidationError,
    WebSocketException,
    WebSocketRequestValidationError,
)
from veloce.helpers import _current_app_var, g
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
)
from veloce.routing.router import RouteInfo
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from veloce._pipeline import WsHandshakeChecks


# Pre-encoded ASCII bytes for the content-type strings the built-in
# response classes emit. Hit at ASGI emit time before the per-request
# `_reject_header_crlf(...).encode()` round-trip; values here are
# trusted (they originate from response.py class definitions) so the
# CRLF/NUL check is skipped on cache hit. Mutation of the cached
# strings is impossible - str is immutable - so a handler-side write
# like `response.content_type = "text/csv"` falls through to the
# uncached path and is validated as before.
_CT_BYTES_CACHE: dict[str, bytes] = {
    MIME_JSON: MIME_JSON.encode("ascii"),
    MIME_HTML: MIME_HTML.encode("ascii"),
    MIME_TEXT_PLAIN_UTF8: MIME_TEXT_PLAIN_UTF8.encode("ascii"),
    MIME_OCTET: MIME_OCTET.encode("ascii"),
}

# Pre-encoded ASCII bytes for small content-length values. Body sizes
# below 2048 cover the entire json-hello / path-param hot path and the
# vast majority of typical JSON API responses; larger payloads fall
# through to the per-request `str(n).encode()` allocation.
_CL_BYTES_SMALL: tuple[bytes, ...] = tuple(str(i).encode("ascii") for i in range(2048))


def _build_asgi_headers(
    headers: Any, skip_content_length: bool
) -> tuple[list[tuple[bytes, bytes]], bool, bool]:
    """Build ASGI `(name, value)` header tuples from a response header map.

    Single source of truth for the ASGI emit header scan shared by the
    streaming and buffered branches of `_asgi_app`. Both paths bypass
    `Response.encode()`, so the response-splitting CRLF guard must be applied
    here. Each header becomes its own tuple; `Set-Cookie` is split back into
    per-cookie tuples (`Response.set_cookie` joins them with a
    `\r\nSet-Cookie: ` literal for the raw HTTP/1.1 wire path). Returns the
    tuples plus whether the response already carried content-type /
    content-length, so the caller can decide on framework defaults. The
    streaming branch passes `skip_content_length=True` (the ASGI server frames
    the body) and ignores the returned flags.
    """
    has_ct = False
    has_cl = False
    asgi_headers: list[tuple[bytes, bytes]] = []
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "set-cookie":
            for piece in v.split("\r\nSet-Cookie:"):
                cookie = piece.strip()
                _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                asgi_headers.append(
                    (RAW_HEADER_SET_COOKIE, _encode_header_value(cookie).encode("latin-1"))
                )
        else:
            if k_lower == "content-type":
                has_ct = True
            elif k_lower == "content-length":
                has_cl = True
                if skip_content_length:
                    continue
            _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
            _reject_header_crlf(v, f"{k} header value")
            asgi_headers.append((k_lower.encode(), _encode_header_value(v).encode("latin-1")))
    return asgi_headers, has_ct, has_cl


# `BaseExceptionGroup` is a builtin only from Python 3.11; on 3.10 the name is
# absent, so resolve it once via `builtins` and degrade to re-raising the first
# failure when grouping is unavailable. Used to surface every error raised while
# unwinding the lifespan stack instead of letting the first one mask the rest.
_BaseExceptionGroup: type[BaseException] | None = getattr(builtins, "BaseExceptionGroup", None)


class AsgiMixin:
    """The ASGI transport + response-emit layer, mixed into Veloce."""

    if TYPE_CHECKING:
        # Attributes / methods the host application (Veloce) provides.
        config: Any
        logger: Any
        match: Callable[..., Any]
        handle_request: Callable[..., Any]
        _ensure_pipeline: Callable[..., Any]
        _setup_openapi: Callable[..., Any]
        _openapi_setup: bool
        _match_asgi_mount: Callable[..., Any]
        _run_lifecycle: Callable[..., Any]
        _pipeline: Any
        _gen: int
        _asgi_stack: Any
        _asgi_stack_gen: int
        _override_subplans: Any
        _dependency_overrides: Any

    async def _emit_413(self, send: Callable, limit: int) -> None:
        """Emit a 413 response directly over ASGI.

        Used by the incremental body-size guard in `__call__`, which
        runs before a `Request` object exists.
        """
        resp = JSONResponse(
            {
                "detail": MSG_REQUEST_BODY_EXCEEDS_MAX,
                "status_code": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "limit": limit,
            },
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
        body = resp.body
        await send(
            {
                "type": ASGI_EVENT_HTTP_RESPONSE_START,
                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "headers": [
                    (RAW_HEADER_CONTENT_TYPE, resp.content_type.encode()),
                    (RAW_HEADER_CONTENT_LENGTH, str(len(body)).encode()),
                ],
            }
        )
        await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": body})

    async def _emit_400(self, send: Callable, detail: str) -> None:
        """Emit a 400 response directly over ASGI.

        Used for malformed request lines that fail before a `Request` object
        exists, such as a `query_string` carrying raw non-ASCII bytes.
        """
        resp = JSONResponse(
            {"detail": detail, "status_code": status.HTTP_400_BAD_REQUEST},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        body = resp.body
        await send(
            {
                "type": ASGI_EVENT_HTTP_RESPONSE_START,
                "status": status.HTTP_400_BAD_REQUEST,
                "headers": [
                    (RAW_HEADER_CONTENT_TYPE, resp.content_type.encode()),
                    (RAW_HEADER_CONTENT_LENGTH, str(len(body)).encode()),
                ],
            }
        )
        await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": body})

    def _build_asgi_stack(self, cp: CompiledPipeline) -> Callable:
        """Wrap the core ASGI app with the compiled PH_ASGI_WRAP chain.

        The fused wrap slot is flattened into one ordered `(cls, options)`
        chain - the highest-`order` wrapper (the live-otel span) first - and
        composed inside out, so that wrapper ends up outermost: it sees the
        request first and the response last, exactly as the historical
        `_asgi_middleware.insert(0, ...)` guaranteed.
        """
        app: Callable = self._asgi_app
        for cls, options in reversed(flatten_asgi_wrap(cp.asgi_wrap)):
            app = cls(app, **options)
        return app

    async def _run_websocket(self, ws: WebSocket, route_info: RouteInfo) -> None:
        """Run a matched WebSocket handler and apply the close-code mapping.

        The connection envelope (host/Origin checks, route match, connection
        refusal) is the caller's responsibility - the ASGI branch drives it via
        receive/send, the native upgrade handler via the raw transport. The
        caller must have set `ws.path_params` and `ws.scope` before invoking
        this. A generic handler exception is re-raised after closing with 1011
        so the surrounding driver can log it.
        """
        # Bind the app context for this connection so handlers, dependencies,
        # and helpers (`current_app`, `g`, template rendering, context
        # processors) work the same under `Veloce.run()` (native upgrade) as
        # under uvicorn/hypercorn (ASGI). Both call sites are independent tasks,
        # so the contextvar set here is scoped to the dispatch task and falls
        # through naturally when it ends - mirroring the HTTP dispatch pattern.
        _current_app_var.set(self)
        g._reset()
        # A fresh resolver per connection: a WebSocket is long-lived,
        # so its yield-dependency teardown stack must not be cleared
        # by a concurrent request resetting the shared HTTP resolver.
        ws_resolver = DependencyResolver()
        ws_resolver._overrides = self._dependency_overrides
        ws_resolver._override_subplans = self._override_subplans
        ws_exc: BaseException | None = None
        try:
            handler = route_info.handler
            # WebSocket DI runs through the shared HandlerPlan /
            # DependencyResolver - the same path as HTTP dispatch - so
            # WebSocket dependencies get `yield`-style teardown and
            # `Security` / `SecurityScopes` support (F8).
            if route_info.handler_plan is not None:
                try:
                    kwargs = await ws_resolver.resolve_ws_plan(
                        route_info.handler_plan,
                        ws,
                        ws.path_params,
                        route_info.route_dep_plans,
                    )
                except RequestValidationError as exc:
                    # A WebSocket dependency failed validation -
                    # surface it as the WS-specific error (V9).
                    raise WebSocketRequestValidationError(getattr(exc, "errors", []) or []) from exc
            else:
                kwargs = {}
            await handler(**kwargs)
        except WebSocketRequestValidationError:
            # Dependency validation failure - close with 1008
            # (policy violation), not 1011, and swallow.
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        except WebSocketException as exc:
            # Application-driven close - send the requested code +
            # reason and swallow the exception (not an error).
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=exc.code, reason=exc.reason or "")
        except Exception as exc:
            ws_exc = exc
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1011_INTERNAL_ERROR)  # internal error
            raise
        else:
            # Clean exit. On the raw path a peer-initiated close has set
            # `_closed` but the server still owes its reply close frame, so the
            # `_needs_close` predicate (not the raw `_closed` flag) drives the
            # reply that completes the RFC 6455 Sec. 5.5.1 handshake.
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close()
        finally:
            # Drain any `yield`-style dependency teardowns the
            # handshake set up, exception-aware. `run_teardowns` now
            # re-raises aggregated teardown failures; log them here so a
            # broken teardown is observable without tearing down the
            # connection-close path itself.
            try:
                await ws_resolver.run_teardowns(ws_exc)
            except Exception:
                self.logger.exception("yield-dependency teardown raised")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """ASGI interface - allows running under uvicorn/hypercorn if desired.

        Any third-party ASGI middleware registered via `add_middleware` (and the
        live-otel span) wraps the core application here; with none registered the
        compiled wrap slot is `None` and this is a direct call to `_asgi_app`
        with no measurable overhead.
        """
        # Inline the pipeline generation check on the ASGI hot path: once setup
        # latches, `_pipeline` is valid and `cp.gen == self._gen`, so this is a
        # cached attribute read plus one int compare with no method-call frame.
        # The cold (re)compile is delegated to `_ensure_pipeline`.
        cp = self._pipeline
        if cp is None or cp.gen != self._gen:
            cp = self._ensure_pipeline()
        if cp.asgi_wrap is not None:
            # Rebuild the wrapper stack only when the pipeline generation moved
            # (a wrapper was registered); otherwise reuse the memoised stack. The
            # wrapped `_asgi_app` re-resolves the pipeline on entry (its `cp`
            # defaults to None), so a same-request late registration in DEBUG /
            # TESTING is still observed - the reason this is not short-circuited.
            stack = self._asgi_stack
            if stack is None or self._asgi_stack_gen != cp.gen:
                stack = self._build_asgi_stack(cp)
                self._asgi_stack = stack
                self._asgi_stack_gen = cp.gen
            await stack(scope, receive, send)
        else:
            # Thread the already-resolved pipeline into the core app so the HTTP
            # dispatch reuses it instead of running a second generation check.
            await self._asgi_app(scope, receive, send, cp)

    async def _asgi_app(
        self,
        scope: dict,
        receive: Callable,
        send: Callable,
        cp: CompiledPipeline | None = None,
    ) -> None:
        """The core ASGI application - HTTP / WebSocket / lifespan handling.

        `cp` is the compiled pipeline resolved by `__call__`; threading it in
        lets the HTTP path skip a redundant generation check. It is `None` when
        a wrapper in the ASGI stack calls this method directly.
        """
        if cp is None:
            cp = self._ensure_pipeline()
        if not self._openapi_setup:
            self._setup_openapi()

        # Mounted arbitrary ASGI apps are dispatched here with the raw
        # scope - the matched prefix is moved from `path` to `root_path`.
        if cp.has_asgi_mounts and scope["type"] in (ASGI_SCOPE_HTTP, ASGI_SCOPE_WEBSOCKET):
            mount = self._match_asgi_mount(scope.get("path", ""))
            if mount is not None:
                prefix, mounted = mount
                sub_scope = dict(scope)
                sub_scope["path"] = scope["path"][len(prefix) :] or "/"
                sub_scope["root_path"] = scope.get("root_path", "") + prefix
                # Drop the now-stale absolute `raw_path`; the mounted app
                # falls back to the rewritten `path`.
                sub_scope.pop("raw_path", None)
                await mounted(sub_scope, receive, send)
                return

        if scope["type"] == ASGI_SCOPE_HTTP:
            # Hand the raw ASGI `(bytes, bytes)` header list to `Request`
            # untouched; the CIMultiDict + per-tuple latin-1 decode is
            # deferred until the handler reads `request.headers`. The
            # hot json-hello / path-param path never reads them.
            raw_headers = scope.get("headers", [])

            # MAX_CONTENT_LENGTH: declared value refused up front; the
            # running total catches chunked bodies that omit it. The check
            # walks raw bytes tuples rather than forcing the Headers build.
            max_size = self.config.get("MAX_CONTENT_LENGTH")
            if max_size is not None:
                declared_b: bytes | None = None
                # ASGI mandates lowercase header names, but `.lower()`
                # defends against a non-compliant server before we trust
                # the declared length. The loop only runs when
                # `MAX_CONTENT_LENGTH` is configured (cold on the hot path).
                for _hk, _hv in raw_headers:
                    if _hk.lower() == RAW_HEADER_CONTENT_LENGTH:
                        declared_b = _hv
                        break
                if declared_b is not None:
                    try:
                        over = int(declared_b) > max_size
                    except ValueError:
                        over = False
                    if over:
                        await self._emit_413(send, max_size)
                        return

            # Common case - one body chunk, no `more_body`. Skip the
            # body_parts list + join.
            message = await receive()
            body = message.get("body", b"") or b""
            if message.get("more_body", False):
                body_parts = [body] if body else []
                received = len(body)
                while True:
                    message = await receive()
                    chunk = message.get("body", b"")
                    if chunk:
                        body_parts.append(chunk)
                        received += len(chunk)
                        if max_size is not None and received > max_size:
                            await self._emit_413(send, max_size)
                            return
                    if not message.get("more_body", False):
                        break
                body = b"".join(body_parts)
            elif max_size is not None and len(body) > max_size:
                await self._emit_413(send, max_size)
                return

            # ASGI HTTP scope mandates `path` and `query_string` keys -
            # direct subscript skips the `.get(default)` default-arg pop.
            path = scope["path"]
            # A well-formed query string is percent-encoded ASCII (RFC 3986
            # Sec. 3.4); raw non-ASCII bytes are a client error, so emit a 400
            # rather than letting `UnicodeDecodeError` escape as a 500. The
            # native path is already protected by httptools' own callback guard.
            try:
                query = scope["query_string"].decode("ascii")
            except UnicodeDecodeError:
                await self._emit_400(send, MSG_INVALID_QUERY_STRING)
                return

            request = Request(
                method=scope["method"],
                path=path,
                query_string=query,
                headers=raw_headers,
                body=body,
                scope=scope,
            )

            response = await self.handle_request(request, cp)

            # Streaming response - emit the body as a sequence of ASGI
            # `http.response.body` chunks instead of one buffered
            # payload. No `content-length`: the ASGI server frames it.
            if response.is_streamed:
                # CRLF-validate every header value - the ASGI emit path
                # bypasses `Response.encode()`, so the splitting guard must
                # be applied here too. Built-in content types hit the cache.
                _ct = response.content_type
                _ct_bytes = _CT_BYTES_CACHE.get(_ct)
                if _ct_bytes is None:
                    _ct_bytes = _reject_header_crlf(_ct, "content-type").encode()
                stream_headers, _, _ = _build_asgi_headers(
                    response.headers, skip_content_length=True
                )
                # ASGI does not mandate header order, so append (O(1)) rather
                # than insert at the front (O(n) list shift).
                stream_headers.append((RAW_HEADER_CONTENT_TYPE, _ct_bytes))
                await send(
                    {
                        "type": ASGI_EVENT_HTTP_RESPONSE_START,
                        "status": response.status_code,
                        "headers": stream_headers,
                    }
                )
                if scope["method"] != HTTP_METHOD_HEAD:
                    async for chunk in getattr(response, "_stream"):  # noqa: B009
                        await send(
                            {
                                "type": ASGI_EVENT_HTTP_RESPONSE_BODY,
                                "body": chunk.encode("utf-8") if isinstance(chunk, str) else chunk,
                                "more_body": True,
                            }
                        )
                await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": b"", "more_body": False})
                return

            # Bodiless statuses (1xx interim, 204, 205, 304) MUST NOT carry a
            # payload (RFC 9110 Sec. 15.2 / 15.3.5 / 15.3.6 / 15.4.5). Strip the
            # body before sending and, below, suppress the framework-default
            # content-type so a `JSONResponse(204)` does not advertise
            # `application/json` over zero bytes.
            body_allowed = status.status_permits_body(response.status_code)
            # A 304 (like HEAD) may carry the would-be-200 Content-Length while
            # sending no body (RFC 9110 Sec. 8.6 / 15.4.5); 1xx/204/205 have no
            # representation, so their length is 0.
            is_304 = response.status_code == status.HTTP_304_NOT_MODIFIED
            advertised_length = len(response.body) if (body_allowed or is_304) else 0
            body_out = response.body if body_allowed else b""

            # RFC 9110 Sec. 9.3.2: HEAD responses must not include a payload
            # body, but `Content-Length` (and other content-related headers)
            # should still reflect the size the equivalent GET would have
            # produced. Blank the body but keep the advertised length, same as
            # the 304 case above.
            head_content_length: int | None = None
            if scope["method"] == HTTP_METHOD_HEAD or is_304:
                head_content_length = advertised_length
                body_out = b""

            # Build the ASGI header list. Each header MUST be its own
            # `(name, value)` tuple; multiple cookies (`Set-Cookie`) get one
            # tuple each. `Response.set_cookie` joins multiple cookies into
            # one header value with `\r\nSet-Cookie: ` literal for the raw
            # HTTP/1.1 wire path; split that back into per-cookie tuples
            # here so the ASGI contract is honoured.
            content_length = (
                head_content_length if head_content_length is not None else len(body_out)
            )
            # CRLF-validate every header value - the ASGI emit path
            # bypasses `Response.encode()`, so the response-splitting
            # guard must be applied here too. Built-in content types and
            # small content-length values hit the precomputed caches.
            _ct = response.content_type
            _ct_bytes = _CT_BYTES_CACHE.get(_ct)
            if _ct_bytes is None:
                _ct_bytes = _reject_header_crlf(_ct, "content-type").encode()
            _cl_bytes = (
                _CL_BYTES_SMALL[content_length]
                if 0 <= content_length < 2048
                else str(content_length).encode("ascii")
            )
            # Single pass over the response headers: emit each as an ASGI
            # tuple while tracking whether a content-type / content-length
            # was supplied, so the framework default is only prepended when
            # the response does not already carry it. The buffered path keeps
            # any response-set content-length (e.g. the compressed length from
            # `GZipMiddleware`), so it does not skip that header.
            if response.headers:
                asgi_headers, has_ct, has_cl = _build_asgi_headers(
                    response.headers, skip_content_length=False
                )
            else:
                has_ct = False
                has_cl = False
                asgi_headers = []
            # Prepend the framework default content-type/content-length only
            # when the response does not already carry that header. A user or
            # middleware value (e.g. the compressed length from
            # `GZipMiddleware`) was emitted above and wins; prepending the
            # default too would put a duplicate header on the wire.
            if not has_cl:
                # ASGI does not mandate header order, so append (O(1)) rather
                # than insert at the front (O(n) list shift), matching the
                # streaming branch above.
                asgi_headers.append((RAW_HEADER_CONTENT_LENGTH, _cl_bytes))
            # Never default a content-type onto a bodiless response (an explicit
            # handler-set content-type still survives via has_ct).
            if not has_ct and body_allowed:
                asgi_headers.append((RAW_HEADER_CONTENT_TYPE, _ct_bytes))

            await send(
                {
                    "type": ASGI_EVENT_HTTP_RESPONSE_START,
                    "status": response.status_code,
                    "headers": asgi_headers,
                }
            )
            await send(
                {
                    "type": ASGI_EVENT_HTTP_RESPONSE_BODY,
                    "body": body_out,
                }
            )

        elif scope["type"] == ASGI_SCOPE_WEBSOCKET:
            # ASGI WS dispatch (W1). Match the route table for a
            # WEBSOCKET-method handler and run it with a WebSocket built
            # from the ASGI receive/send pair. Path params are coerced
            # the same way they are for HTTP. The app context
            # (`_current_app_var` / `g`) is bound inside `_run_websocket`,
            # shared with the native upgrade path; the host/Origin checks
            # below do not read it.

            # Host and Origin validation for WebSocket handshakes - an HTTP
            # middleware such as TrustedHostMiddleware or
            # WebSocketOriginMiddleware never sees a `websocket` scope, so
            # apply any host allow-list and Origin allow-list directly here.
            # The compiled pipeline pre-filters the `(is_host_allowed,
            # is_websocket_origin_allowed)` pairs from the middleware once, so
            # the per-connect path iterates a frozen tuple instead of probing
            # every middleware. `None` (no middleware) skips the gate entirely.
            ws_checks: WsHandshakeChecks | None = cp.ws_handshake
            if ws_checks is not None:
                ws_host = ""
                ws_origin = ""
                _host_seen = False
                _origin_seen = False
                for _hk, _hv in scope.get("headers", []):
                    # First occurrence of each header wins - a duplicate
                    # `Origin` must not be able to shadow the real one.
                    if _hk == b"host" and not _host_seen:
                        ws_host = _extract_host(_hv.decode("latin-1"))
                        _host_seen = True
                    elif _hk == b"origin" and not _origin_seen:
                        ws_origin = _hv.decode("latin-1")
                        _origin_seen = True
                for _host_check, _origin_check in ws_checks:
                    if _host_check is not None and not _host_check(ws_host):
                        msg = await receive()
                        if msg["type"] == ASGI_EVENT_WS_CONNECT:
                            await send(
                                {
                                    "type": ASGI_EVENT_WS_CLOSE,
                                    "code": status.WS_1008_POLICY_VIOLATION,
                                }
                            )
                        return
                    if _origin_check is not None and not _origin_check(ws_origin):
                        msg = await receive()
                        if msg["type"] == ASGI_EVENT_WS_CONNECT:
                            await send(
                                {
                                    "type": ASGI_EVENT_WS_CLOSE,
                                    "code": status.WS_1008_POLICY_VIOLATION,
                                }
                            )
                        return

            ws_match = self.match(ROUTE_METHOD_WEBSOCKET, scope.get("path", "/"))
            if ws_match is None:
                # No handler - refuse the connection per ASGI WS spec.
                msg = await receive()
                if msg["type"] == ASGI_EVENT_WS_CONNECT:
                    await send(
                        {"type": ASGI_EVENT_WS_CLOSE, "code": status.WS_1008_POLICY_VIOLATION}
                    )
                return

            ws = WebSocket.from_asgi(scope, receive, send)
            ws.path_params = ws_match.path_params
            route_info = ws_match.route_info
            await self._run_websocket(ws, route_info)

        elif scope["type"] == ASGI_SCOPE_LIFESPAN:
            while True:
                message = await receive()
                if message["type"] == ASGI_EVENT_LIFESPAN_STARTUP:
                    try:
                        await self._run_lifecycle(LIFECYCLE_STARTUP)
                        await send({"type": ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE})
                    except Exception as exc:
                        await send(
                            {"type": ASGI_EVENT_LIFESPAN_STARTUP_FAILED, "message": str(exc)}
                        )
                        return
                elif message["type"] == ASGI_EVENT_LIFESPAN_SHUTDOWN:
                    # Mirror the startup branch: a teardown that raises (an
                    # `on_shutdown` handler, the lifespan CM `__aexit__`, or a
                    # drained spawned task) is reported via the spec's
                    # `lifespan.shutdown.failed` message with a full traceback,
                    # rather than escaping `__call__` and leaving the server to
                    # drain on an unhandled exception. `_run_lifecycle` already
                    # runs every teardown before re-raising, so the failed
                    # signal does not skip remaining cleanups.
                    try:
                        await self._run_lifecycle(LIFECYCLE_SHUTDOWN)
                        await send({"type": ASGI_EVENT_LIFESPAN_SHUTDOWN_COMPLETE})
                    except BaseException:
                        await send(
                            {
                                "type": ASGI_EVENT_LIFESPAN_SHUTDOWN_FAILED,
                                "message": traceback.format_exc(),
                            }
                        )
                    return
