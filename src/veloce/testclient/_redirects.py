"""Redirect following for the test client."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any
from urllib.parse import urlparse

from veloce._constants import (
    HEADER_LOCATION,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
)
from veloce.status import (
    HTTP_301_MOVED_PERMANENTLY,
    HTTP_302_FOUND,
    HTTP_303_SEE_OTHER,
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_308_PERMANENT_REDIRECT,
)
from veloce.testclient._body import StreamBody
from veloce.testclient._headers import _HeadersArg
from veloce.testclient.response import TestResponse


def _resolve_redirect_location(location: str, base_url: str) -> tuple[str, str]:
    """Decompose a `Location` header into `(path, query)` for the next hop.

    With no live network an absolute URL to another host cannot be followed; a
    same-host one reduces to `path?query#fragment` and a relative one passes
    through.
    """
    parsed = urlparse(location)
    if parsed.scheme and parsed.netloc:
        own_host = urlparse(base_url).netloc
        if parsed.netloc != own_host:
            raise RuntimeError(
                f"TestClient cannot follow redirect to absolute URL {location!r} "
                f"on a different host (test client host is {own_host!r})"
            )
        # Fragments are stripped by browsers on redirect - drop them.
        return parsed.path or "/", parsed.query
    # Relative location (or odd scheme-only) - use verbatim, splitting any qs.
    path, _, query = location.partition("?")
    return path, query


# Redirect status codes the test clients follow when `follow_redirects` is on.
_REDIRECT_STATUSES = frozenset(
    (
        HTTP_301_MOVED_PERMANENTLY,
        HTTP_302_FOUND,
        HTTP_303_SEE_OTHER,
        HTTP_307_TEMPORARY_REDIRECT,
        HTTP_308_PERMANENT_REDIRECT,
    )
)


# 301/302/303 rewrite the next hop to GET with an empty body; 307/308 preserve.
_REDIRECT_TO_GET = frozenset((HTTP_301_MOVED_PERMANENTLY, HTTP_302_FOUND, HTTP_303_SEE_OTHER))


async def _follow_redirects(
    *,
    build_headers: Callable[[_HeadersArg], list[tuple[str, str]]],
    send_one: Callable[
        [str, str, str, list[tuple[str, str]], bytes, Any | None], Awaitable[TestResponse]
    ],
    update_cookies: Callable[[TestResponse], None],
    max_redirects: int,
    base_url: str,
    follow: bool,
    client_name: str,
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    headers: _HeadersArg,
    stream: StreamBody | None,
) -> TestResponse:
    """Drive the request/redirect loop shared by both test clients.

    `send_one` is the per-hop async dispatch; how that coroutine is awaited is
    the only difference between the two clients. The status classification,
    GET-rewrite, replay guard and hop cap live here, so a redirect-semantics fix
    lands once.

    RFC 9110 Sec. 15.4: 303 always rewrites the method to GET and drops the body,
    and 301/302 follow the same browser convention here; 307/308 preserve both.
    """
    current_method = method
    current_path = path
    current_query = query_string
    current_body = body
    current_headers = headers
    current_stream = stream

    for _ in range(max_redirects + 1):
        all_headers = build_headers(current_headers)
        resp = await send_one(
            current_method,
            current_path,
            current_query,
            all_headers,
            current_body,
            current_stream,
        )
        update_cookies(resp)
        if not follow or resp.status_code not in _REDIRECT_STATUSES:
            return resp
        location = resp.headers.get(HEADER_LOCATION)
        if not location:
            return resp
        if resp.status_code in _REDIRECT_TO_GET:
            current_method = HTTP_METHOD_GET
            current_body = b""
            # The body is dropped on this hop, so the one-shot stream iterator
            # is no longer needed (and must not be re-consumed).
            current_stream = None
        elif current_stream is not None:
            # 307/308 must replay method and body, and a consumed one-shot iterator
            # cannot be re-read - so fail loudly rather than send an empty body.
            raise RuntimeError(
                "streamed request body cannot be replayed across redirects; "
                "pass follow_redirects=False"
            )
        current_path, current_query = _resolve_redirect_location(location, base_url)
    raise RuntimeError(
        f"{client_name} exceeded {max_redirects} redirects following {method} {path}"
    )
