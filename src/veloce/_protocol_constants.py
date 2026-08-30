"""Protocol vocabulary — internal protocol and routing constants.

These names centralize wire-level ASGI tokens, HTTP method names, auth-scheme
names, and a few framework-level protocol invariants that are repeated across
multiple modules, plus the small `build_trace_carrier` tail that assembles a
W3C trace-carrier dict from extracted header values (colocated with the trace
header constants so the keys and their consumer stay together).

This module is internal-only and is not part of the public API.
"""

from __future__ import annotations

# ASGI scope types
ASGI_SCOPE_HTTP = "http"
ASGI_SCOPE_WEBSOCKET = "websocket"
ASGI_SCOPE_LIFESPAN = "lifespan"

# ASGI event/message types
ASGI_EVENT_HTTP_REQUEST = "http.request"
ASGI_EVENT_HTTP_DISCONNECT = "http.disconnect"
ASGI_EVENT_HTTP_RESPONSE_START = "http.response.start"
ASGI_EVENT_HTTP_RESPONSE_BODY = "http.response.body"
ASGI_EVENT_WS_ACCEPT = "websocket.accept"
ASGI_EVENT_WS_CONNECT = "websocket.connect"
ASGI_EVENT_WS_RECEIVE = "websocket.receive"
ASGI_EVENT_WS_SEND = "websocket.send"
ASGI_EVENT_WS_CLOSE = "websocket.close"
ASGI_EVENT_WS_DISCONNECT = "websocket.disconnect"
ASGI_EVENT_LIFESPAN_STARTUP = "lifespan.startup"
ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE = "lifespan.startup.complete"
ASGI_EVENT_LIFESPAN_STARTUP_FAILED = "lifespan.startup.failed"
ASGI_EVENT_LIFESPAN_SHUTDOWN = "lifespan.shutdown"
ASGI_EVENT_LIFESPAN_SHUTDOWN_COMPLETE = "lifespan.shutdown.complete"
ASGI_EVENT_LIFESPAN_SHUTDOWN_FAILED = "lifespan.shutdown.failed"

# Standard HTTP methods
HTTP_METHOD_CONNECT = "CONNECT"
HTTP_METHOD_DELETE = "DELETE"
HTTP_METHOD_GET = "GET"
HTTP_METHOD_HEAD = "HEAD"
HTTP_METHOD_OPTIONS = "OPTIONS"
HTTP_METHOD_PATCH = "PATCH"
HTTP_METHOD_POST = "POST"
HTTP_METHOD_PUT = "PUT"
HTTP_METHOD_QUERY = "QUERY"
HTTP_METHOD_TRACE = "TRACE"

# Framework-internal routing pseudo-method
ROUTE_METHOD_WEBSOCKET = "WEBSOCKET"

# Auth / OAuth2 tokens
AUTH_SCHEME_BASIC = "Basic"
AUTH_SCHEME_BEARER = "Bearer"
AUTH_SCHEME_DIGEST = "Digest"
OAUTH2_GRANT_TYPE_PASSWORD = "password"

# URL / transport schemes
URL_SCHEME_HTTP = "http"
URL_SCHEME_HTTPS = "https"
URL_SCHEME_WS = "ws"
URL_SCHEME_WSS = "wss"

# The schemes that mean "this connection is encrypted". Single source, so a
# guard cannot recognise one of them and miss the other: an HTTPS redirect that
# knew `wss` while `Request.is_secure` did not gave two answers about one
# connection. Compared against the already-lowercased `URL.scheme`; RFC 3986
# Sec. 3.1 makes a scheme case-insensitive, so normalisation happens once where
# the scheme is resolved rather than at each comparison.
SECURE_URL_SCHEMES = frozenset({URL_SCHEME_HTTPS, URL_SCHEME_WSS})

# Lower-case raw header bytes used on ASGI / protocol paths
RAW_HEADER_CONTENT_LENGTH = b"content-length"
RAW_HEADER_CONTENT_TYPE = b"content-type"
RAW_HEADER_SET_COOKIE = b"set-cookie"

# Framework lifecycle events
LIFECYCLE_STARTUP = "startup"
LIFECYCLE_SHUTDOWN = "shutdown"

# Internal multi-cookie join separator
SET_COOKIE_JOINER = "\r\nSet-Cookie: "

# W3C trace-context header names
TRACE_HEADER_TRACEPARENT = "traceparent"
TRACE_HEADER_TRACESTATE = "tracestate"


def build_trace_carrier(traceparent: str | None, tracestate: str | None) -> dict[str, str] | None:
    """Assemble a W3C trace-carrier dict from extracted header values.

    Shared tail for the two extraction sites (the framework-core
    `request.headers` reader and the optional otel bridge's raw-ASGI-scope
    reader): both pull the same two headers from different sources, then build
    the same `{traceparent[, tracestate]}` carrier. Returns `None` when
    `traceparent` is absent so callers can cheaply skip propagator extraction.
    """
    if traceparent is None:
        return None
    carrier = {TRACE_HEADER_TRACEPARENT: traceparent}
    if tracestate is not None:
        carrier[TRACE_HEADER_TRACESTATE] = tracestate
    return carrier
