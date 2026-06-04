"""Internal protocol and routing vocabulary constants.

These names centralize wire-level ASGI tokens, HTTP method names, auth-scheme
names, and a few framework-level protocol invariants that are repeated across
multiple modules.

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

# Lower-case raw header bytes used on ASGI / protocol paths
RAW_HEADER_CONTENT_LENGTH = b"content-length"
RAW_HEADER_CONTENT_TYPE = b"content-type"
RAW_HEADER_SET_COOKIE = b"set-cookie"

# W3C trace-context header names
TRACE_HEADER_TRACEPARENT = "traceparent"
TRACE_HEADER_TRACESTATE = "tracestate"

# Framework lifecycle events
LIFECYCLE_STARTUP = "startup"
LIFECYCLE_SHUTDOWN = "shutdown"

# Internal multi-cookie join separator
SET_COOKIE_JOINER = "\r\nSet-Cookie: "
