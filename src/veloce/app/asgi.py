"""ASGI transport — the `__call__` entry, dispatch handoff, and response emit.

A mixin on `Veloce`, covering HTTP and websocket dispatch and the buffered and
streaming emit paths. `__call__` resolves the compiled pipeline and either threads
it into `_asgi_app` (the common no-wrapper case) or runs the ASGI wrapper stack;
`_asgi_app` builds the `Request`, hands HTTP off to `handle_request` and websockets
to `_run_websocket`, and writes the response bytes. The pre-encoded emit helpers
live here too so this module owns the whole emit path.
"""

from __future__ import annotations

import contextlib
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import veloce._constants as _constants
import veloce.status as status
from veloce._constants import (
    MIME_TEXT_PLAIN_UTF8,
    MSG_INVALID_QUERY_STRING,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
)
from veloce._internal import (
    _LABEL_HEADER_VALUE_SUFFIX,
    MIME_HTML,
    MIME_JSON,
    MIME_OCTET,
    _current_app_var,
    _current_request_var,
    _encode_header_value,
    _extract_host,
    _reject_header_crlf,
    dumps_for,
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
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    RAW_HEADER_CONTENT_LENGTH,
    RAW_HEADER_CONTENT_TYPE,
    RAW_HEADER_SET_COOKIE,
    ROUTE_METHOD_WEBSOCKET,
    SET_COOKIE_JOINER,
)
from veloce.app._host import AppHost
from veloce.dependency import DependencyResolver
from veloce.exceptions import (
    RequestValidationError,
    WebSocketException,
    WebSocketRequestValidationError,
)
from veloce.helpers import g
from veloce.http._body import ASGIBodySource
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
    advertised_length,
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
_CL_SMALL_MAX = 2048
_CL_BYTES_SMALL: tuple[bytes, ...] = tuple(str(i).encode("ascii") for i in range(_CL_SMALL_MAX))


# Encoded `(name, value) -> (name_bytes, value_bytes)` pairs, memoised across
# responses. A response header is overwhelmingly one a middleware precomputed
# once - the security headers, a CORS policy - so the same two `str` objects
# arrive on every request and the per-header scan collapses to one dict hit.
# CPython caches a string's hash on the object, so the tuple key costs two
# cached hashes and a pointer comparison for those identical objects.
#
# Bounded because a value CAN differ per response (an `ETag`, a `Location`, a
# request id). Those miss, refill, and eventually trip the cap; clearing outright
# rather than evicting one entry keeps the miss path to a single branch, and a
# miss is still cheaper than the previous code because the CRLF scan below is
# inline either way.
_ENCODED_HEADER_PAIRS: dict[tuple[str, str], tuple[bytes, bytes]] = {}
_MAX_ENCODED_PAIRS = 512


# Methods that do not carry a request body, so the declared-`Content-Length`
# walk has nothing to find. RFC 9110 permits a body on these, which is why the
# received-length checks still run - this only skips the early reject.
_BODILESS_METHODS = frozenset((HTTP_METHOD_GET, HTTP_METHOD_HEAD, HTTP_METHOD_OPTIONS))


# Every response header name reaching the ASGI transport must be lowercase
# bytes. The set of names a response actually carries is overwhelmingly the
# framework's own constants, and `.encode()` on each of them ran per header per
# response. Built from every `HEADER_*` name constant in `_constants`, so a
# constant added there is picked up without editing this table, and never
# written at runtime, so an application setting dynamic header names cannot
# grow it without bound - those simply fall through to `.encode()`.
_ENCODED_HEADER_NAMES: dict[str, bytes] = {
    value.lower(): value.lower().encode("latin-1")
    for name, value in vars(_constants).items()
    if name.startswith("HEADER_") and not name.startswith("HEADER_VALUE_")
}


def _content_type_bytes(content_type: str) -> bytes:
    """Return the content-type as guarded ASCII bytes, cached when it is known.

    The ASGI emit path bypasses `Response.encode()`, so the response-splitting
    guard it would have applied belongs here instead. One function for all three
    emit sites: the cold one encoded bare, and shipped a CR/LF-bearing value as a
    raw ASGI header.
    """
    cached = _CT_BYTES_CACHE.get(content_type)
    if cached is not None:
        return cached
    return _reject_header_crlf(content_type, "content-type").encode()


def _build_asgi_headers(headers: Any) -> tuple[list[tuple[bytes, bytes]], bool, bool]:
    """Build ASGI `(name, value)` header tuples from a response header map.

    Single source of truth for the ASGI emit header scan shared by the
    streaming and buffered branches of `_asgi_app`. Both paths bypass
    `Response.encode()`, so the response-splitting CRLF guard must be applied
    here. Each header becomes its own tuple; `Set-Cookie` is split back into
    per-cookie tuples (`Response.set_cookie` joins them with a
    `SET_COOKIE_JOINER` literal for the raw HTTP/1.1 wire path). Returns the
    tuples plus whether the response already carried content-type /
    content-length, so the caller can decide on framework defaults.
    """
    has_ct = False
    has_cl = False
    asgi_headers: list[tuple[bytes, bytes]] = []
    # `Response.headers` is a case-SENSITIVE dict, but HTTP field names are
    # case-insensitive (RFC 9110 Sec. 5.1): `Content-Security-Policy` and
    # `content-security-policy` are one field, and emitting both would ship a
    # duplicate that browsers intersect to the most restrictive - silently
    # narrowing the policy the later writer intended. Track where each folded
    # name was emitted so a second spelling overwrites the first rather than
    # appending beside it. `Set-Cookie` is excluded: it is legitimately
    # multi-valued, so every entry is emitted.
    slot_by_name: dict[str, int] = {}
    encoded_pairs = _ENCODED_HEADER_PAIRS
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "set-cookie":
            # The same constant `Response.set_cookie` joins with. A literal here
            # matched only because it is a prefix of that constant and the strip
            # below hid the difference; changing the constant would have left this
            # path silently emitting several cookies inside one header.
            for piece in v.split(SET_COOKIE_JOINER):
                cookie = piece.strip()
                _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                asgi_headers.append(
                    (RAW_HEADER_SET_COOKIE, _encode_header_value(cookie).encode("latin-1"))
                )
            continue
        if k_lower == "content-type":
            has_ct = True
        elif k_lower == "content-length":
            has_cl = True
        entry = encoded_pairs.get((k, v))
        if entry is None:
            # The CRLF test is three C-level substring scans. Running them inline
            # and entering `_reject_header_crlf` only when one hits keeps four
            # Python-level calls per header off the emit path, while the helper
            # still owns the message so there is exactly one wording.
            if "\r" in k or "\n" in k or "\x00" in k:
                _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
            # The label is joined only when raising; building `f"{k} header
            # value"` here made a string per header per response that almost
            # every call discards.
            if "\r" in v or "\n" in v or "\x00" in v:
                _reject_header_crlf(v, k, _LABEL_HEADER_VALUE_SUFFIX)
            encoded_name = _ENCODED_HEADER_NAMES.get(k_lower)
            if encoded_name is None:
                encoded_name = k_lower.encode("latin-1")
            entry = (encoded_name, _encode_header_value(v).encode("latin-1"))
            if len(encoded_pairs) >= _MAX_ENCODED_PAIRS:
                encoded_pairs.clear()
            encoded_pairs[(k, v)] = entry
        slot = slot_by_name.get(k_lower)
        if slot is None:
            slot_by_name[k_lower] = len(asgi_headers)
            asgi_headers.append(entry)
        else:
            asgi_headers[slot] = entry
    return asgi_headers, has_ct, has_cl


async def _refuse_websocket(
    receive: Callable,
    send: Callable,
    scope: dict | None = None,
    status_code: int | None = None,
) -> None:
    """Refuse an ASGI WebSocket handshake per the spec.

    Drain the `websocket.connect`, then refuse. A `status_code` is sent as a
    real pre-accept HTTP response when the server advertises the ASGI
    `websocket.http.response` extension, so an unknown path answers 404 the way
    the built-in server does; without the extension - and for a policy refusal
    with no status of its own - the refusal is a `websocket.close` with the
    policy-violation code 1008, which a server renders as 403.

    A close frame was the only refusal this path could make, so every reason
    reached the client as one status while the built-in server distinguished
    them.
    """
    msg = await receive()
    if msg["type"] != ASGI_EVENT_WS_CONNECT:
        return
    if status_code is not None and scope is not None:
        extensions = scope.get("extensions") or {}
        if "websocket.http.response" in extensions:
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": status_code,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "websocket.http.response.body", "body": b""})
            return
    await send({"type": ASGI_EVENT_WS_CLOSE, "code": status.WS_1008_POLICY_VIOLATION})


class AsgiMixin(AppHost):
    """The ASGI transport + response-emit layer, mixed into Veloce."""

    # ── Refusals and direct emits ───────────────────────

    async def _refuse_too_large(
        self,
        send: Any,
        method: str,
        path: str,
        query: str,
        raw_headers: Any,
        scope: Any,
        cp: Any,
        max_size: int | None,
    ) -> None:
        """Emit the `413` for an over-`MAX_CONTENT_LENGTH` body.

        One place rather than three. The body may be refused at three points -
        on a declared `Content-Length`, on the running total of a buffered read,
        and on the running total of a streamed one - and each built the same
        throwaway `Request` to give the response phase something to run against.
        A refusal path that is written out per site is how one of them ends up
        fixed and the others not, which is what happened to the native
        transport's own 413.
        """
        # The response phase runs against this request, so it has to look like
        # one a response middleware can work with. Without `app` and the two
        # contextvars, any `process_response` calling `current_app` raised
        # `RuntimeError: Working outside of application context` and that
        # escaped to the client in place of the 413.
        #
        # This binds only what the *response* phase reads. The request phase is
        # still not run - refusing before reading the body is the point of this
        # path - so a middleware that caches state in `process_request` will not
        # find it here, and must fall back, as `CORSMiddleware` does for the
        # `Origin` header.
        request = Request(method, path, query, raw_headers, b"", scope=scope)
        request.app = self
        _current_app_var.set(self)
        _current_request_var.set(request)
        await self._emit_response(
            send,
            await self._body_too_large_response(request, cp, max_size),
        )

    async def _emit_response(self, send: Callable, response: Response) -> None:
        """Emit an already-built `Response` over ASGI, headers included.

        The cold reject path. It carries the response's own headers - which is
        what lets a rejection that ran the response phase ship its CORS and
        security headers, rather than reaching the client as an opaque
        cross-origin failure with no `Access-Control-Allow-Origin` on it.

        Deliberately separate from the buffered emit in `__call__`: that one is
        the hot path and is written inline against precomputed caches, and
        routing a reject through it would mean restructuring it. The split is
        intentional, but the *rules* must not diverge - keep the bodiless-status
        handling below in step with the buffered branch in `_asgi_app`.
        """
        body = response.body
        headers, has_ct, has_cl = (
            _build_asgi_headers(response.headers) if response.headers else ([], False, False)
        )
        # A bodiless status carries no payload and no default content-type
        # (RFC 9110 Sec. 15.3.5 / 15.3.6 / 15.4.5), the same rule the buffered
        # branch applies. Every current caller of this helper uses a
        # body-permitting status, so nothing reaches it today - but this is the
        # general "emit an already-built Response" path, and the next caller
        # should not have to know the two branches disagreed.
        body_allowed = status.status_permits_body(response.status_code)
        # RFC 9110 Sec. 8.6 / 15.4.5: a 304 sends no body but may carry the
        # `Content-Length` the equivalent 200 would have, which is what both
        # `Response.encode` and the buffered branch advertise. 1xx, 204 and 205
        # have no representation, so their length is 0.
        length = advertised_length(response.status_code, body)
        if not body_allowed:
            body = b""
        if not has_cl and length is not None:
            headers.append((RAW_HEADER_CONTENT_LENGTH, str(length).encode("ascii")))
        # An explicit handler-set content-type still survives via `has_ct`.
        if not has_ct and body_allowed:
            headers.append((RAW_HEADER_CONTENT_TYPE, _content_type_bytes(response.content_type)))
        await send(
            {
                "type": ASGI_EVENT_HTTP_RESPONSE_START,
                "status": response.status_code,
                "headers": headers,
            }
        )
        await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": body})

    async def _emit_error(self, send: Callable, status_code: int, payload: dict) -> None:
        """Emit a JSON error response directly over ASGI, in the app's dialect.

        Encoded through `dumps_for(self, ...)` rather than by letting
        `JSONResponse` resolve it: this runs before dispatch binds the app
        contextvar, so `dumps_current` found nothing and fell back to the direct
        encoder. The app is not missing here - it is `self` - it simply was not
        bound yet, and the result was a refusal whose shape depended on whether an
        earlier request had happened to leave the contextvar set on this task.
        """
        response = JSONResponse._from_encoded(dumps_for(self, payload))
        response.status_code = status_code
        await self._emit_response(send, response)

    async def _emit_400(self, send: Callable, detail: str) -> None:
        """Emit a 400 response directly over ASGI.

        Used for malformed request lines that fail before a `Request` object
        exists, such as a `query_string` carrying raw non-ASCII bytes.
        """
        await self._emit_error(
            send,
            status.HTTP_400_BAD_REQUEST,
            {"detail": detail, "status_code": status.HTTP_400_BAD_REQUEST},
        )

    # ── The middleware stack ────────────────────────────

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

    # ── WebSocket dispatch ──────────────────────────────

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
        # Mirror `Request.app` so a WebSocket handler reaches its application
        # directly, not only through the `current_app` proxy.
        ws.app = self
        # Apply the configured idle timeout here rather than at each transport's
        # construction site: only the native one read the config, so the single
        # config-driven way to reap a silent peer was inert on the transport most
        # apps deploy. This is the one funnel every WebSocket handler passes
        # through, so a transport added later inherits it. Guarded, so an
        # explicit `from_asgi(idle_timeout=...)` still wins over config, and a
        # handler's own `set_idle_timeout` still overrides both.
        if ws._idle_timeout is None:
            configured = self.config.get("WEBSOCKET_IDLE_TIMEOUT")
            if configured is not None:
                ws.set_idle_timeout(configured)
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
            # Recorded before the close below, because the close awaits: a peer
            # that has already gone makes the native driver cancel this task
            # mid-handshake, and the re-raise never runs. The driver reads this
            # back so the failure is reported instead of vanishing with the
            # cancellation. Under an ASGI server the re-raise reaches the
            # server, which logs it, so nothing is logged here.
            ws._handler_exc = exc
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1011_INTERNAL_ERROR)
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

    # ── The ASGI entry points ───────────────────────────

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
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
        """Run the core ASGI application - HTTP, WebSocket and lifespan handling.

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
            declared_over = False
            if max_size is not None:
                declared_b: bytes | None = None
                # Compared as received: ASGI mandates lowercase header names, so
                # a compliant server matches here, and the config ships a default
                # so this walks every header of every request - a `.lower()` per
                # header is a bytes allocation each time, on requests that mostly
                # carry no `Content-Length` at all.
                #
                # A server that violates that mandate loses only the early
                # rejection, not the limit: both body branches below enforce the
                # received length on their own (a running total across chunks,
                # and `ASGIBodySource` for a streamed body), so an oversized body
                # is still refused - just after the first read rather than before
                # it.
                # A method that does not carry a body skips the walk entirely.
                # This is only the *early* reject: the received-length checks
                # below enforce the cap on their own (a running total across the
                # eager drain's chunks, and `ASGIBodySource` for a streamed
                # body), so a bodiless-method request that does send an
                # over-limit body is still refused - just after the first read
                # rather than before it, exactly as for a chunked body that
                # omits the header. Measured on techc: ~160ns saved per GET
                # against ~28ns added per POST, on an ~8-header request.
                if scope["method"] not in _BODILESS_METHODS:
                    for _hk, _hv in raw_headers:
                        if _hk == RAW_HEADER_CONTENT_LENGTH:
                            declared_b = _hv
                            break
                if declared_b is not None:
                    try:
                        over = int(declared_b) > max_size
                    except ValueError:
                        over = False
                    declared_over = over

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

            # A body over the limit is refused through the same builder the
            # dispatch path uses, on a real `Request`, so the rejection runs the
            # response phase: it carries the app's CORS and security headers and
            # states one payload shape. Emitting it before a `Request` existed is
            # why a cross-origin upload that tripped the limit reached the client
            # as an opaque CORS failure rather than as a 413. Paid only by a
            # request that is being refused.
            if declared_over:
                await self._refuse_too_large(
                    send, scope["method"], path, query, raw_headers, scope, cp, max_size
                )
                return

            # Match the route ONCE here and thread the result into
            # `handle_request` so it is not matched again in dispatch. This lets
            # a non-streaming route (the common case) keep the eager inline body
            # drain below - byte-for-byte the pre-streaming path, zero added
            # per-request cost - while only a `stream=True` route is built over a
            # lazy `ASGIBodySource` it consumes incrementally via
            # `request.stream()`. The lazy source enforces the running-total cap
            # itself (raising `RequestEntityTooLarge`); the eager path enforces it
            # in the read loop. The declared-Content-Length reject above guards
            # both before any body byte is read.
            method = scope["method"]
            match = self.match(method, path)
            # A path under a mount matches nothing here - the route lives in the
            # sub-app - so the eager drain below would run before anything could
            # ask whether that route streams, and a `stream=True` route silently
            # became a buffered one once its app was mounted. Hand the mounted
            # path a lazy source instead and let the mount dispatch decide: it
            # drains for a buffered sub-route, exactly as this branch would have.
            mounted = match is None and cp.has_mounted_apps and self._path_under_mount(path)
            if mounted or (match is not None and match.route_info.stream):
                request = Request(
                    method=method,
                    path=path,
                    query_string=query,
                    headers=raw_headers,
                    body=b"",
                    body_source=ASGIBodySource(receive, max_size),
                    scope=scope,
                )
            else:
                # Eager drain. Common case - one body chunk, no `more_body` -
                # skips the body_parts list + join.
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
                                await self._refuse_too_large(
                                    send, method, path, query, raw_headers, scope, cp, max_size
                                )
                                return
                        if not message.get("more_body", False):
                            break
                    body = b"".join(body_parts)
                elif max_size is not None and len(body) > max_size:
                    await self._refuse_too_large(
                        send, method, path, query, raw_headers, scope, cp, max_size
                    )
                    return
                request = Request(
                    method=method,
                    path=path,
                    query_string=query,
                    headers=raw_headers,
                    body=body,
                    scope=scope,
                )

            # Both the declared Content-Length and the received bytes were
            # checked above, for the buffered and the streaming branch alike, so
            # dispatch does not repeat either. The flag is per-request: a
            # mounted sub-app is dispatched with a fresh `Request` and is still
            # checked against its own limit.
            request._length_enforced = True

            response = await self.handle_request(request, cp, match)

            # Streaming response - emit the body as a sequence of ASGI
            # `http.response.body` chunks instead of one buffered
            # payload. No `content-length`: the ASGI server frames it.
            if response.is_streamed:
                # A streamed body usually has no length to advertise, but a
                # file's is known from its stat - keep the one the response set
                # so a streamed download stays length-delimited rather than
                # silently becoming a chunked one.
                # Guarded like the buffered branch below: an empty mapping has
                # nothing to build, and the call is not free.
                if response.headers:
                    stream_headers, _, _ = _build_asgi_headers(response.headers)
                else:
                    stream_headers = []
                # A bodiless status carries no payload and no default
                # content-type, the same rule the buffered branch below
                # applies. Without it a streamed 204 shipped its chunks, and a
                # client on a keep-alive connection read them as the start of
                # the next response.
                _body_allowed = status.status_permits_body(response.status_code)
                if _body_allowed:
                    # CRLF-validate every header value - the ASGI emit path
                    # bypasses `Response.encode()`, so the splitting guard must
                    # be applied here too. Built-in content types hit the cache.
                    _ct_bytes = _content_type_bytes(response.content_type)
                    # ASGI does not mandate header order, so append (O(1))
                    # rather than insert at the front (O(n) list shift).
                    stream_headers.append((RAW_HEADER_CONTENT_TYPE, _ct_bytes))
                await send(
                    {
                        "type": ASGI_EVENT_HTTP_RESPONSE_START,
                        "status": response.status_code,
                        "headers": stream_headers,
                    }
                )
                if _body_allowed and scope["method"] != HTTP_METHOD_HEAD:
                    async for chunk in response._stream:
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
            length = advertised_length(response.status_code, response.body)
            body_out = response.body if body_allowed else b""

            # RFC 9110 Sec. 9.3.2: HEAD responses must not include a payload
            # body, but `Content-Length` (and other content-related headers)
            # should still reflect the size the equivalent GET would have
            # produced. Blank the body but keep the advertised length, same as
            # the 304 case above.
            head_content_length: int | None = None
            if (
                scope["method"] == HTTP_METHOD_HEAD
                or response.status_code == status.HTTP_304_NOT_MODIFIED
            ):
                head_content_length = length
                body_out = b""

            # Build the ASGI header list. Each header MUST be its own
            # `(name, value)` tuple; multiple cookies (`Set-Cookie`) get one
            # tuple each. `Response.set_cookie` joins multiple cookies into
            # one header value with `\r\nSet-Cookie: ` literal for the raw
            # HTTP/1.1 wire path; split that back into per-cookie tuples
            # here so the ASGI contract is honoured.
            # `None` only when a 304 downgraded from a stream has no knowable
            # length; the header is then omitted rather than claiming zero.
            content_length = (
                (head_content_length if head_content_length is not None else len(body_out))
                if (head_content_length is not None or length is not None)
                else None
            )
            # CRLF-validate every header value - the ASGI emit path
            # bypasses `Response.encode()`, so the response-splitting
            # guard must be applied here too. Built-in content types and
            # small content-length values hit the precomputed caches.
            _ct_bytes = _content_type_bytes(response.content_type)
            _cl_bytes = (
                None
                if content_length is None
                else _CL_BYTES_SMALL[content_length]
                if 0 <= content_length < _CL_SMALL_MAX
                else str(content_length).encode("ascii")
            )
            # Single pass over the response headers: emit each as an ASGI
            # tuple while tracking whether a content-type / content-length
            # was supplied, so the framework default is only prepended when
            # the response does not already carry it. The buffered path keeps
            # any response-set content-length (e.g. the compressed length from
            # `GZipMiddleware`), so it does not skip that header.
            if response.headers:
                asgi_headers, has_ct, has_cl = _build_asgi_headers(response.headers)
            else:
                has_ct = False
                has_cl = False
                asgi_headers = []
            # Prepend the framework default content-type/content-length only
            # when the response does not already carry that header. A user or
            # middleware value (e.g. the compressed length from
            # `GZipMiddleware`) was emitted above and wins; prepending the
            # default too would put a duplicate header on the wire.
            if not has_cl and _cl_bytes is not None:
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
            await self._asgi_websocket(scope, receive, send, cp)

        elif scope["type"] == ASGI_SCOPE_LIFESPAN:
            await self._asgi_lifespan(receive, send)

    async def _asgi_websocket(
        self, scope: Any, receive: Any, send: Any, cp: CompiledPipeline
    ) -> None:
        """Serve one ASGI `websocket` scope.

        Out of line from `_asgi_app` deliberately. Everything the HTTP branch
        does is inline because it runs per request; this runs **once per
        connection**, before any frame moves, so the call costs nothing that can
        be measured and the HTTP path is 200 lines shorter to read.
        """
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
                    await _refuse_websocket(receive, send)
                    return
                if _origin_check is not None and not _origin_check(ws_origin):
                    await _refuse_websocket(receive, send)
                    return

        ws_match = self.match(ROUTE_METHOD_WEBSOCKET, scope.get("path", "/"))
        if ws_match is None:
            # No handler. A 404 says so where the server can carry one,
            # matching the built-in server; otherwise the spec's close.
            await _refuse_websocket(receive, send, scope, status.HTTP_404_NOT_FOUND)
            return

        ws = WebSocket.from_asgi(scope, receive, send)
        ws.path_params = ws_match.path_params
        route_info = ws_match.route_info
        await self._run_websocket(ws, route_info)

    async def _asgi_lifespan(self, receive: Any, send: Any) -> None:
        """Serve the ASGI `lifespan` scope: the startup/shutdown message loop.

        Runs **twice per process**. Inline it bought nothing and cost the reader
        of the HTTP branch thirty lines.
        """
        while True:
            message = await receive()
            if message["type"] == ASGI_EVENT_LIFESPAN_STARTUP:
                try:
                    await self._run_lifecycle(LIFECYCLE_STARTUP)
                    await send({"type": ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE})
                except Exception as exc:
                    await send({"type": ASGI_EVENT_LIFESPAN_STARTUP_FAILED, "message": str(exc)})
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
