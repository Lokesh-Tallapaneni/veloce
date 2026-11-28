"""Test client — in-memory driver for the app's ASGI surface.

Constructs ASGI scopes directly and calls `app.__call__(scope, receive, send)`
on a dedicated event loop, so the radix router, dependency resolver, middleware
chain, response encoder and lifespan handshake all run as they would under a
production server. Reached through `app.test_client()` or the `TestClient`
class; the scope shape, message names and header byte-list layout follow the
ASGI specification.

The package splits along its seams: `client` and `async_client` (the two
clients), `response` (`TestResponse`), `_ws_session` (the WebSocket session),
and the private helpers for bodies, cookies, headers, redirects and ASGI
plumbing. Every name the single module exposed still imports from
`veloce.testclient`.
"""

from __future__ import annotations

from veloce.testclient._asgi import (
    _abort_self_pipe_on_close,
    _build_scope,
    _ClientChannel,
    _new_loop,
)
from veloce.testclient._body import (
    StreamBody,
    _aiter_body_chunks,
    _prepare_body_request,
)
from veloce.testclient._cookies import _decode_cookie_value
from veloce.testclient.async_client import AsyncTestClient
from veloce.testclient.client import TestClient
from veloce.testclient.response import TestResponse

__all__ = [
    "AsyncTestClient",
    "StreamBody",
    "TestClient",
    "TestResponse",
    "_ClientChannel",
    "_abort_self_pipe_on_close",
    "_aiter_body_chunks",
    "_build_scope",
    "_decode_cookie_value",
    "_new_loop",
    "_prepare_body_request",
]
