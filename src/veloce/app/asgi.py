"""ASGI transport — the `__call__` entry, dispatch handoff, and response emit.

A mixin on `Veloce`, covering HTTP, websocket and lifespan dispatch together with
the buffered and streaming emit paths, so this module owns the whole emit path.
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
    BadRequest,
    RequestValidationError,
    WebSocketException,
    WebSocketRequestValidationError,
)
from veloce.helpers import g
from veloce.http._body import ASGIBodySource
from veloce.http.request import Request
from veloce.http.response import (
    Response,
    advertised_length,
)
from veloce.routing.router import RouteInfo
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from veloce._pipeline import WsHandshakeChecks


# Trusted values from response.py, so a hit skips the CRLF/NUL check.
_CT_BYTES_CACHE: dict[str, bytes] = {
    MIME_JSON: MIME_JSON.encode("ascii"),
    MIME_HTML: MIME_HTML.encode("ascii"),
    MIME_TEXT_PLAIN_UTF8: MIME_TEXT_PLAIN_UTF8.encode("ascii"),
    MIME_OCTET: MIME_OCTET.encode("ascii"),
}


# Pre-encoded content-length bytes; a larger value pays `str(n).encode()`.
_CL_SMALL_MAX = 2048
_CL_BYTES_SMALL: tuple[bytes, ...] = tuple(str(i).encode("ascii") for i in range(_CL_SMALL_MAX))


# Encoded `(name, value)` pairs, so a precomputed header is one dict hit.
_ENCODED_HEADER_PAIRS: dict[tuple[str, str], tuple[bytes, bytes]] = {}
_MAX_ENCODED_PAIRS = 512


# RFC 9110 permits a body on these, so this skips only the declared walk.
_BODILESS_METHODS = frozenset((HTTP_METHOD_GET, HTTP_METHOD_HEAD, HTTP_METHOD_OPTIONS))


# Built from every `HEADER_*` constant; never written at runtime.
_ENCODED_HEADER_NAMES: dict[str, bytes] = {
    value.lower(): value.lower().encode("latin-1")
    for name, value in vars(_constants).items()
    if name.startswith("HEADER_") and not name.startswith("HEADER_VALUE_")
}


def _content_type_bytes(content_type: str) -> bytes:
    """Return the content-type as guarded ASCII bytes, cached when it is known.

    The ASGI emit path bypasses `Response.encode()`, so its response-splitting
    guard belongs here instead.
    """
    cached = _CT_BYTES_CACHE.get(content_type)
    if cached is not None:
        return cached
    return _reject_header_crlf(content_type, "content-type").encode()


def _build_asgi_headers(headers: Any) -> tuple[list[tuple[bytes, bytes]], bool, bool]:
    """Build ASGI `(name, value)` header tuples from a response header map.

    Both emit paths bypass `Response.encode()`, so the response-splitting CRLF
    guard must be applied here. `Set-Cookie` is split back into per-cookie
    tuples. Returns the tuples plus whether the response already carried
    content-type / content-length, so the caller can decide on defaults.
    """
    has_ct = False
    has_cl = False
    asgi_headers: list[tuple[bytes, bytes]] = []
    # `Response.headers` is case-SENSITIVE, but HTTP field names are not
    # (RFC 9110 Sec. 5.1), and a duplicate browsers intersect narrows the policy.
    slot_by_name: dict[str, int] = {}
    encoded_pairs = _ENCODED_HEADER_PAIRS
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "set-cookie":
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
            # Scanned inline, so the label is joined only when raising.
            if "\r" in k or "\n" in k or "\x00" in k:
                _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
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


async def _emit_stream(send: Callable[..., Any], response: Response, method: str | None) -> None:
    """Write a streamed response over ASGI: headers, chunks, terminal frame.

    The one writer for both streaming paths. No `Content-Length` is added - the
    server frames the chunks - but one the response already set is kept, so a
    file whose size its stat gave stays length-delimited rather than chunked.
    """
    if response.headers:
        headers, has_ct, _ = _build_asgi_headers(response.headers)
    else:
        headers, has_ct = [], False
    # A bodiless status carries no payload and no default content-type
    # (RFC 9110 Sec. 15.3.5 / 15.3.6 / 15.4.5).
    allowed = status.status_permits_body(response.status_code)
    if allowed and not has_ct:
        # Appended (O(1)) rather than inserted at the front (O(n) list shift).
        headers.append((RAW_HEADER_CONTENT_TYPE, _content_type_bytes(response.content_type)))
    await send(
        {
            "type": ASGI_EVENT_HTTP_RESPONSE_START,
            "status": response.status_code,
            "headers": headers,
        }
    )
    # A `HEAD` carries its `GET`'s headers and no body (RFC 9110 Sec. 9.3.2).
    if allowed and method != HTTP_METHOD_HEAD:
        async for chunk in response._stream:
            await send(
                {
                    "type": ASGI_EVENT_HTTP_RESPONSE_BODY,
                    "body": chunk.encode("utf-8") if isinstance(chunk, str) else chunk,
                    "more_body": True,
                }
            )
    await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": b"", "more_body": False})


async def _refuse_websocket(
    receive: Callable[..., Any],
    send: Callable[..., Any],
    scope: dict[str, Any] | None = None,
    status_code: int | None = None,
) -> None:
    """Refuse an ASGI WebSocket handshake per the spec.

    Drain the `websocket.connect`, then refuse. A `status_code` is sent as a
    real pre-accept HTTP response when the server advertises the ASGI
    `websocket.http.response` extension; otherwise the refusal is a
    `websocket.close` with the policy-violation code 1008.
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

    def _refusal_request(
        self, method: str, path: str, query: str, raw_headers: Any, scope: Any
    ) -> Request:
        """Build the throwaway `Request` a pre-dispatch refusal is answered against.

        Binds the app and the two contextvars so the response phase has
        something a response middleware can work with. The request phase is
        still not run - refusing before reading the body is the point - so
        middleware that caches state in `process_request` must fall back, as
        `CORSMiddleware` does for the `Origin` header.
        """
        request = Request(method, path, query, raw_headers, b"", scope=scope)
        request.app = self
        _current_app_var.set(self)
        _current_request_var.set(request)
        return request

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

        One place for all three refusal points: a declared `Content-Length` and
        the running total of a buffered or a streamed read.
        """
        request = self._refusal_request(method, path, query, raw_headers, scope)
        await self._emit_response(
            send,
            await self._body_too_large_response(request, cp, max_size),
            method,
        )

    async def _refuse_bad_request(
        self, send: Any, detail: str, method: str, path: str, raw_headers: Any, scope: Any, cp: Any
    ) -> None:
        """Emit the `400` for a request line that failed before dispatch.

        Through the app's error handlers, like every other refusal: a malformed
        query string is as much a client error as a missing route.
        """
        request = self._refusal_request(method, path, "", raw_headers, scope)
        await self._emit_response(
            send,
            await self._http_exception_response(request, BadRequest(detail), cp),
            method,
        )

    async def _emit_response(
        self, send: Callable[..., Any], response: Response, method: str | None = None
    ) -> None:
        """Emit an already-built `Response` over ASGI, headers included.

        The cold reject path. It carries the response's own headers, so a
        rejection that ran the response phase ships its CORS and security
        headers rather than reaching the client as an opaque cross-origin
        failure. Separate from the hot buffered emit in `_asgi_app`, whose
        bodiless-status handling must stay in step with this.
        """
        if response.is_streamed:
            # An error handler may return a `StreamingResponse` or `FileResponse`.
            await _emit_stream(send, response, method)
            return

        body = response.body
        headers, has_ct, has_cl = (
            _build_asgi_headers(response.headers) if response.headers else ([], False, False)
        )
        # A bodiless status carries no payload and no default content-type
        # (RFC 9110 Sec. 15.3.5 / 15.3.6 / 15.4.5); a `HEAD` no body (Sec. 9.3.2).
        body_allowed = status.status_permits_body(response.status_code)
        send_body = body_allowed and method != HTTP_METHOD_HEAD
        # RFC 9110 Sec. 8.6 / 15.4.5: a 304 sends no body but may carry the
        # `Content-Length` a 200 would; 1xx, 204 and 205 have length 0.
        length = advertised_length(response.status_code, body, body_allowed)
        if not send_body:
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

    # ── The middleware stack ────────────────────────────

    def _build_asgi_stack(self, cp: CompiledPipeline) -> Callable[..., Any]:
        """Wrap the core ASGI app with the compiled PH_ASGI_WRAP chain.

        The fused wrap slot is flattened into one ordered `(cls, options)`
        chain, highest-`order` first, and composed inside out so that wrapper
        ends up outermost.
        """
        app: Callable[..., Any] = self._asgi_app
        for cls, options in reversed(flatten_asgi_wrap(cp.asgi_wrap)):
            app = cls(app, **options)
        return app

    # ── WebSocket dispatch ──────────────────────────────

    async def _run_websocket(self, ws: WebSocket, route_info: RouteInfo) -> None:
        """Run a matched WebSocket handler and apply the close-code mapping.

        The connection envelope (host/Origin checks, route match, refusal) is
        the caller's responsibility, and the caller must set `ws.path_params`
        and `ws.scope` first. A generic handler exception is re-raised after
        closing with 1011 so the surrounding driver can log it.
        """
        # So handlers behave the same under `Veloce.run()` as under an ASGI server.
        _current_app_var.set(self)
        ws.app = self
        # At the one funnel every handler passes through; explicit wins.
        if ws._idle_timeout is None:
            configured = self.config.get("WEBSOCKET_IDLE_TIMEOUT")
            if configured is not None:
                ws.set_idle_timeout(configured)
        g._reset()
        # Fresh per connection, so a concurrent request cannot clear its stack.
        ws_resolver = DependencyResolver()
        ws_resolver._overrides = self._dependency_overrides
        ws_resolver._override_subplans = self._override_subplans
        ws_exc: BaseException | None = None
        try:
            handler = route_info.handler
            # The shared resolver, so `yield` teardown and `Security` work here.
            if route_info.handler_plan is not None:
                try:
                    kwargs = await ws_resolver.resolve_ws_plan(
                        route_info.handler_plan,
                        ws,
                        ws.path_params,
                        route_info.route_dep_plans,
                    )
                except RequestValidationError as exc:
                    raise WebSocketRequestValidationError(getattr(exc, "errors", []) or []) from exc
            else:
                kwargs = {}
            await handler(**kwargs)
        except WebSocketRequestValidationError:
            # A validation failure is a policy violation (1008), not 1011.
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        except WebSocketException as exc:
            # An application-driven close is not an error, so it is swallowed.
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=exc.code, reason=exc.reason or "")
        except Exception as exc:
            ws_exc = exc
            # Before the close, which awaits: a gone peer cancels this task.
            ws._handler_exc = exc
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1011_INTERNAL_ERROR)
            raise
        else:
            # `_needs_close`, not `_closed`: a peer-initiated close still owes the
            # reply frame that completes the RFC 6455 Sec. 5.5.1 handshake.
            if ws._needs_close:
                with contextlib.suppress(Exception):
                    await ws.close()
        finally:
            # Logged, not raised: a broken teardown must not break the close path.
            try:
                await ws_resolver.run_teardowns(ws_exc)
            except Exception:
                self.logger.exception("yield-dependency teardown raised")

    # ── The ASGI entry points ───────────────────────────

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        """ASGI interface - allows running under uvicorn/hypercorn if desired.

        Any third-party ASGI middleware registered via `add_middleware` wraps the
        core application here; with none registered the compiled wrap slot is
        `None` and this is a direct call to `_asgi_app`.
        """
        # Inlined: once setup latches this is an attribute read and an int compare.
        cp = self._pipeline
        if cp is None or cp.gen != self._gen:
            cp = self._ensure_pipeline()
        if cp.asgi_wrap is not None:
            # Rebuilt only when the generation moved; `_asgi_app` re-resolves.
            stack = self._asgi_stack
            if stack is None or self._asgi_stack_gen != cp.gen:
                stack = self._build_asgi_stack(cp)
                self._asgi_stack = stack
                self._asgi_stack_gen = cp.gen
            await stack(scope, receive, send)
        else:
            # Threaded in so HTTP dispatch does not repeat the generation check.
            await self._asgi_app(scope, receive, send, cp)

    async def _asgi_app(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
        cp: CompiledPipeline | None = None,
    ) -> None:
        """Run the core ASGI application - HTTP, WebSocket and lifespan handling.

        `cp` is the compiled pipeline resolved by `__call__`, threaded in so the
        HTTP path skips a redundant generation check; `None` when a wrapper in
        the ASGI stack calls this directly.
        """
        if cp is None:
            cp = self._ensure_pipeline()
        if not self._openapi_setup:
            self._setup_openapi()

        # A mounted ASGI app gets the raw scope, prefix moved to `root_path`.
        if cp.has_asgi_mounts and scope["type"] in (ASGI_SCOPE_HTTP, ASGI_SCOPE_WEBSOCKET):
            mount = self._match_asgi_mount(scope.get("path", ""))
            if mount is not None:
                prefix, mounted = mount
                sub_scope = dict(scope)
                sub_scope["path"] = scope["path"][len(prefix) :] or "/"
                sub_scope["root_path"] = scope.get("root_path", "") + prefix
                # Stale now; the mounted app falls back to the rewritten `path`.
                sub_scope.pop("raw_path", None)
                await mounted(sub_scope, receive, send)
                return

        if scope["type"] == ASGI_SCOPE_HTTP:
            # Untouched: the decode is deferred to `request.headers`.
            raw_headers = scope.get("headers", [])

            # The declared value up front; the running total catches a chunked one.
            max_size = self.config.get("MAX_CONTENT_LENGTH")
            declared_over = False
            if max_size is not None:
                declared_b: bytes | None = None
                # Compared as received: ASGI mandates lowercase header names.
                # Measured: ~160ns saved per GET against ~28ns added per POST.
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

            # The ASGI HTTP scope mandates these keys, so subscript over `.get`.
            path = scope["path"]
            # A query string is percent-encoded ASCII (RFC 3986 Sec. 3.4), so raw
            # non-ASCII bytes are a 400, not a `UnicodeDecodeError` as a 500.
            try:
                query = scope["query_string"].decode("ascii")
            except UnicodeDecodeError:
                # Empty rather than a lossy decode of parameters nobody sent.
                await self._refuse_bad_request(
                    send,
                    MSG_INVALID_QUERY_STRING,
                    scope["method"],
                    path,
                    raw_headers,
                    scope,
                    cp,
                )
                return

            # Through the dispatch builder, so the rejection carries CORS headers.
            if declared_over:
                await self._refuse_too_large(
                    send, scope["method"], path, query, raw_headers, scope, cp, max_size
                )
                return

            # Matched ONCE and threaded into `handle_request`.
            method = scope["method"]
            match = self.match(method, path)
            # A path under a mount matches nothing here; the mount decides.
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
                    receive=receive,
                )
            else:
                # Eager drain; one chunk with no `more_body` skips list and join.
                message = await receive()
                body = message.get("body", b"") or b""
                if message.get("more_body", False):
                    body_parts = [body] if body else []
                    received = len(body)
                    if max_size is not None and received > max_size:
                        # The loop below measures only what it appends.
                        await self._refuse_too_large(
                            send, method, path, query, raw_headers, scope, cp, max_size
                        )
                        return
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
                    # The channel a streaming handler polls for the client going.
                    receive=receive,
                )

            # Both branches checked both lengths; per-request, so a mount keeps
            # its own limit.
            request._length_enforced = True

            response = await self.handle_request(request, cp, match)

            if response.is_streamed:
                # Shared with the cold reject path so the rules cannot drift.
                await _emit_stream(send, response, scope["method"])
                return

            # A bodiless status MUST NOT carry a payload (RFC 9110 Sec. 15.2 /
            # 15.3.5 / 15.3.6 / 15.4.5), so body and content-type are suppressed.
            body_allowed = status.status_permits_body(response.status_code)
            length = advertised_length(response.status_code, response.body, body_allowed)
            body_out = response.body if body_allowed else b""

            # RFC 9110 Sec. 9.3.2: a HEAD carries no payload body, but
            # `Content-Length` still reflects the equivalent GET's size.
            head_content_length: int | None = None
            if (
                scope["method"] == HTTP_METHOD_HEAD
                or response.status_code == status.HTTP_304_NOT_MODIFIED
            ):
                head_content_length = length
                body_out = b""

            # Omitted rather than claiming zero for an unknowable length.
            content_length = (
                (head_content_length if head_content_length is not None else len(body_out))
                if (head_content_length is not None or length is not None)
                else None
            )
            # This path bypasses `Response.encode()`, so its guard applies here.
            _ct_bytes = _content_type_bytes(response.content_type)
            _cl_bytes = (
                None
                if content_length is None
                else _CL_BYTES_SMALL[content_length]
                if 0 <= content_length < _CL_SMALL_MAX
                else str(content_length).encode("ascii")
            )
            # A response-set content-length is kept, so the default fills a gap.
            if response.headers:
                asgi_headers, has_ct, has_cl = _build_asgi_headers(response.headers)
            else:
                has_ct = False
                has_cl = False
                asgi_headers = []
            if not has_cl and _cl_bytes is not None:
                asgi_headers.append((RAW_HEADER_CONTENT_LENGTH, _cl_bytes))
            # An explicit handler-set content-type still survives via `has_ct`.
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

        Out of line from `_asgi_app` deliberately: this runs **once per
        connection**, so the call costs nothing measurable.
        """
        # An HTTP middleware never sees a `websocket` scope; `None` skips this.
        ws_checks: WsHandshakeChecks | None = cp.ws_handshake
        if ws_checks is not None:
            ws_host = ""
            ws_origin = ""
            _host_seen = False
            _origin_seen = False
            for _hk, _hv in scope.get("headers", []):
                # First occurrence wins: a duplicate `Origin` must not shadow it.
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
            # A 404 where the server can carry one; otherwise the spec's close.
            await _refuse_websocket(receive, send, scope, status.HTTP_404_NOT_FOUND)
            return

        ws = WebSocket.from_asgi(scope, receive, send)
        ws.path_params = ws_match.path_params
        route_info = ws_match.route_info
        await self._run_websocket(ws, route_info)

    async def _asgi_lifespan(self, receive: Any, send: Any) -> None:
        """Serve the ASGI `lifespan` scope: the startup/shutdown message loop."""
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
                # Reported as `lifespan.shutdown.failed`, not out of `__call__`.
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
