"""One registered handler covers every built-in error, not only the dispatched ones.

`404` and `405` are raised as `HTTPException`, so they reach the registered error
handlers. Three other built-in refusals were written out directly instead: the
`413` for an over-`MAX_CONTENT_LENGTH` body (refusable at three points), the
`429` a rate limiter returns from `process_request`, and the `400` for a query
string that is not ASCII. An application could not put all of them into one
error format, because `@app.exception_handler(HTTPException)` was never
consulted for those three - only an outermost middleware saw all of them, and
only as finished responses.

Each refusal now offers itself to the handler table first and keeps its own body
as the default, so an application that registers nothing sees byte-identical
output. The negative tests below pin those defaults, because "every error is now
reshapeable" is worth nothing if it silently reshaped them for everybody.

The raw ASGI driver is used throughout rather than `TestClient`: two of the five
refusals happen before dispatch (a body refused on its declared length, a query
string that never decoded) and one needs a body with no `Content-Length`, none of
which a client that frames its own requests can produce.
"""

from __future__ import annotations

import json

import pytest

from tests._asgi_drive import body_of, drive, headers_of, http_scope, status_of
from veloce import (
    HTTPException,
    JSONResponse,
    MethodNotAllowed,
    Middleware,
    NotFound,
    RateLimitMiddleware,
    RedirectResponse,
    Request,
    RequestEntityTooLarge,
    Response,
    TooManyRequests,
    Veloce,
    abort,
)

_LIMIT = 10
_OVER = b"x" * (_LIMIT + 6)
# Two chunks, each inside the limit, over it together - the running total is the
# only thing that can refuse this one.
_OVER_IN_CHUNKS = [b"x" * 8, b"x" * 8]
_DEFAULT_413_DETAIL = "Request body exceeds MAX_CONTENT_LENGTH"


class _Stamp(Middleware):
    """Records that the response phase ran, on whatever response reaches it."""

    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers["X-Response-Phase"] = "ran"
        return response


def _build(
    *, broad: bool = False, by_status: bool = False, raising: bool = False, limited: bool = False
) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT
    # Pinned so the raising-handler degradation is asserted rather than the
    # re-raise an implicit DEBUG+TESTING app would take instead.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.add_middleware(_Stamp())
    if limited:
        app.add_middleware(RateLimitMiddleware(max_requests=1, window_seconds=60))

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"size": len(await request.body())}

    async def drain(request: Request) -> dict:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"size": total}

    app.add_route("/drain", drain, methods=["POST"], stream=True)

    if broad:

        @app.exception_handler(HTTPException)
        async def as_problem(request: Request, exc: HTTPException) -> JSONResponse:
            return JSONResponse(
                {"problem": True, "status": exc.status_code},
                status_code=exc.status_code,
            )

    if by_status:

        @app.errorhandler(413)
        async def too_big(request: Request, exc: HTTPException) -> JSONResponse:
            return JSONResponse({"narrow": True}, status_code=exc.status_code)

    if raising:

        @app.exception_handler(HTTPException)
        async def broken(request: Request, exc: HTTPException) -> JSONResponse:
            raise RuntimeError("the error handler itself failed")

    return app


# ── the five refusals, each driven the only way it can be reached ─────


async def _not_found(app: Veloce) -> list[dict]:
    return await drive(app, http_scope(method="GET", path="/nope"))


async def _not_allowed(app: Veloce) -> list[dict]:
    return await drive(app, http_scope(method="DELETE", path="/echo"))


async def _declared_too_large(app: Veloce) -> list[dict]:
    """Refused on the declared `Content-Length`, before a body byte is read."""
    scope = http_scope(
        method="POST",
        path="/echo",
        headers=[(b"content-length", str(len(_OVER)).encode())],
    )
    return await drive(app, scope, body=_OVER)


async def _buffered_too_large(app: Veloce) -> list[dict]:
    """Refused on the running total of the eager drain - no `Content-Length` sent."""
    return await drive(app, http_scope(method="POST", path="/echo"), chunks=_OVER_IN_CHUNKS)


async def _streamed_too_large(app: Veloce) -> list[dict]:
    """Refused on the running total of a `stream=True` route's own read."""
    return await drive(app, http_scope(method="POST", path="/drain"), chunks=_OVER_IN_CHUNKS)


async def _bad_query(app: Veloce) -> list[dict]:
    """A query string carrying raw non-ASCII bytes (RFC 3986 Sec. 3.4)."""
    return await drive(app, http_scope(method="GET", path="/ok", query_string=b"q=\xff"))


async def _too_many(app: Veloce) -> list[dict]:
    """The second request on a `max_requests=1` budget."""
    await drive(app, http_scope(method="GET", path="/ok"))
    return await drive(app, http_scope(method="GET", path="/ok"))


_TOO_LARGE_POINTS = (
    _declared_too_large,
    _buffered_too_large,
    _streamed_too_large,
)


# ── positive: one handler reshapes all of them ───────────────────────


@pytest.mark.parametrize(
    ("refuse", "expected_status"),
    [
        (_not_found, 404),
        (_not_allowed, 405),
        (_declared_too_large, 413),
        (_buffered_too_large, 413),
        (_streamed_too_large, 413),
        (_bad_query, 400),
    ],
)
async def test_a_broad_handler_formats_every_builtin_error(refuse, expected_status):
    messages = await refuse(_build(broad=True))
    assert status_of(messages) == expected_status
    assert json.loads(body_of(messages)) == {"problem": True, "status": expected_status}


async def test_a_broad_handler_formats_a_rate_limit_refusal():
    """Driven on its own app: a `max_requests=1` budget refuses every later request."""
    messages = await _too_many(_build(broad=True, limited=True))
    assert status_of(messages) == 429
    assert json.loads(body_of(messages)) == {"problem": True, "status": 429}


async def test_the_three_body_limit_points_all_reach_the_handler():
    """A refusal written out per site is how one gets fixed and the others do not."""
    app = _build(broad=True)
    bodies = [body_of(await refuse(app)) for refuse in _TOO_LARGE_POINTS]
    assert bodies[0] == bodies[1] == bodies[2]


async def test_a_status_handler_wins_over_the_broad_one():
    """`abort()` semantics: the status-code table is consulted before the class table."""
    messages = await _declared_too_large(_build(broad=True, by_status=True))
    assert status_of(messages) == 413
    assert json.loads(body_of(messages)) == {"narrow": True}


async def test_a_status_handler_leaves_the_other_errors_to_the_broad_one():
    """The narrow registration must not capture every refusal on its way past."""
    messages = await _not_found(_build(broad=True, by_status=True))
    assert json.loads(body_of(messages)) == {"problem": True, "status": 404}


# ── negative: the default bodies, for an app that registers nothing ──


async def test_an_unhandled_404_keeps_its_default_body():
    messages = await _not_found(_build())
    assert status_of(messages) == 404
    assert json.loads(body_of(messages)) == {"detail": "Not Found", "status_code": 404}


async def test_an_unhandled_405_keeps_its_default_body():
    messages = await _not_allowed(_build())
    assert status_of(messages) == 405
    assert json.loads(body_of(messages)) == {
        "detail": "Method Not Allowed",
        "allowed": ["POST"],
    }
    assert headers_of(messages)["allow"] == "POST"


@pytest.mark.parametrize("refuse", _TOO_LARGE_POINTS)
async def test_an_unhandled_413_keeps_its_default_body_including_the_limit(refuse):
    """`limit` is part of the payload - a client reading it must keep finding it."""
    messages = await refuse(_build())
    assert status_of(messages) == 413
    assert json.loads(body_of(messages)) == {
        "detail": _DEFAULT_413_DETAIL,
        "status_code": 413,
        "limit": _LIMIT,
    }


async def test_an_unhandled_400_keeps_its_default_body():
    messages = await _bad_query(_build())
    assert status_of(messages) == 400
    assert json.loads(body_of(messages)) == {
        "detail": "Invalid query string encoding",
        "status_code": 400,
    }


async def test_an_unhandled_429_keeps_its_plain_text_body():
    """The one built-in refusal whose default is not JSON; it stays that way."""
    messages = await _too_many(_build(limited=True))
    assert status_of(messages) == 429
    assert body_of(messages) == b"Too Many Requests"


async def test_an_unhandled_429_still_carries_its_rate_limit_headers():
    headers = headers_of(await _too_many(_build(limited=True)))
    assert int(headers["retry-after"]) > 0
    assert headers["x-ratelimit-limit"] == "1"
    assert headers["x-ratelimit-remaining"] == "0"


async def test_a_handled_429_still_carries_its_rate_limit_headers():
    """A handler owns the body; it must not be able to drop the retry signal."""
    headers = headers_of(await _too_many(_build(broad=True, limited=True)))
    assert int(headers["retry-after"]) > 0
    assert headers["x-ratelimit-limit"] == "1"
    assert headers["x-ratelimit-remaining"] == "0"


# ── negative: a handler that is itself broken ────────────────────────


@pytest.mark.parametrize(
    "refuse", [_not_found, _declared_too_large, _buffered_too_large, _bad_query]
)
async def test_a_raising_handler_degrades_to_500_rather_than_hanging(refuse):
    """A pre-dispatch refusal has no route and no dispatch context to fall back on."""
    messages = await refuse(_build(raising=True))
    assert status_of(messages) == 500
    assert json.loads(body_of(messages)) == {
        "detail": "Internal Server Error",
        "status_code": 500,
    }


async def test_a_raising_handler_leaves_a_rate_limit_refusal_refused():
    """Degrading to 500 must not turn a refusal into a served request."""
    messages = await _too_many(_build(raising=True, limited=True))
    assert status_of(messages) == 500
    assert "retry-after" in headers_of(messages)


# ── regression: the response phase still runs on every refusal ───────


@pytest.mark.parametrize(
    "refuse",
    [_not_found, _not_allowed, _declared_too_large, _buffered_too_large, _streamed_too_large],
)
@pytest.mark.parametrize("broad", [False, True])
async def test_the_response_phase_runs_on_every_refusal(refuse, broad):
    """What an outermost middleware was the only layer able to see all of."""
    headers = headers_of(await refuse(_build(broad=broad)))
    assert headers["x-response-phase"] == "ran"


@pytest.mark.parametrize("broad", [False, True])
async def test_the_response_phase_runs_on_a_rate_limit_refusal(broad):
    headers = headers_of(await _too_many(_build(broad=broad, limited=True)))
    assert headers["x-response-phase"] == "ran"


@pytest.mark.parametrize("broad", [False, True])
async def test_the_response_phase_runs_on_an_unparseable_query_string(broad):
    """It did not before: the 400 was written straight to `send`."""
    headers = headers_of(await _bad_query(_build(broad=broad)))
    assert headers["x-response-phase"] == "ran"


# ── regression: the 405's mandatory Allow header survives a handler ───


def _405_app(handler=None) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/only")
    async def only() -> dict:
        return {"ok": True}

    if handler is not None:
        app.exception_handler(HTTPException)(handler)
    return app


async def _post_to_get_only(app: Veloce) -> list[dict]:
    return await drive(app, http_scope(method="POST", path="/only"))


async def test_a_handled_405_still_carries_allow():
    """RFC 9110 Sec. 15.5.6: `Allow` is a MUST on a 405, handler or not.

    The class handler table reaches the 405 only now that the refusal carries an
    exception, so a broad handler intercepts it where previously the default
    response - which carries the header - was served instead. Without the
    re-apply, registering one broad handler silently drops `Allow` from every
    405 in the application.
    """

    async def as_problem(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    messages = await _post_to_get_only(_405_app(as_problem))
    assert status_of(messages) == 405
    assert headers_of(messages)["allow"] == "GET"
    assert json.loads(body_of(messages)) == {"error": "Method Not Allowed"}


async def test_a_handler_answering_another_status_is_not_given_allow():
    """`Allow` describes a 405; stamping it on a redirect would describe nothing."""

    async def to_login(request: Request, exc: HTTPException) -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    messages = await _post_to_get_only(_405_app(to_login))
    assert status_of(messages) == 302
    assert "allow" not in headers_of(messages)


async def test_a_handler_that_sets_allow_itself_keeps_its_own_value():
    """The re-apply fills a gap; it does not override a deliberate statement.

    Matched case-insensitively, so a handler writing `allow` in any casing is
    honoured rather than having a second `Allow` emitted beside it.
    """

    async def own(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": "x"}, status_code=405, headers={"allow": "PATCH"})

    headers = headers_of(await _post_to_get_only(_405_app(own)))
    assert headers["allow"] == "PATCH"


async def test_an_unhandled_405_still_carries_allow_and_its_default_body():
    """The no-handler path is the one that always worked; it must stay unchanged."""
    messages = await _post_to_get_only(_405_app())
    assert status_of(messages) == 405
    assert headers_of(messages)["allow"] == "GET"
    assert json.loads(body_of(messages)) == {
        "detail": "Method Not Allowed",
        "allowed": ["GET"],
    }


# ── regression: a route's middleware opt-out survives the error path ──


class _Tag(Middleware):
    """Stamps a header named after itself, so exclusion is visible per instance."""

    def __init__(self, tag: str) -> None:
        super().__init__(name=tag)
        self.tag = tag

    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers[f"X-{self.tag}"] = "ran"
        return response


def _excluding_app(*, broad: bool) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.add_middleware(_Tag("kept"))
    app.add_middleware(_Tag("dropped"))

    async def gone() -> dict:
        abort(404, "gone")
        return {}

    app.add_route("/gone", gone, methods=["GET"], exclude_middleware=["dropped"])

    if broad:

        @app.exception_handler(HTTPException)
        async def as_problem(request: Request, exc: HTTPException) -> JSONResponse:
            return JSONResponse({"problem": True}, status_code=exc.status_code)

    return app


def test_an_excluding_route_always_has_a_response_chain_to_filter():
    """The premise that lets the response phase be skipped on an empty chain.

    `_http_exception_response` skips the phase when the fused chain is empty.
    That is only safe because the dynamic filtered walk - which an excluding
    route takes, and which is not a no-op on an empty fused chain - cannot be
    reached with no middleware registered: a route can only exclude middleware
    that exists. Pinned because the guard reads as an optimisation and its
    safety rests entirely on this.
    """
    app = _excluding_app(broad=False)
    assert app._ensure_pipeline().http_post is not None


@pytest.mark.parametrize("broad", [False, True])
async def test_an_excluded_middleware_stays_excluded_on_an_aborted_response(broad):
    """`exclude_middleware` is symmetric across the error path, not only the happy one.

    The response phase for a raised `HTTPException` has to run the route's
    filtered chain, not the app-wide fused one. Both branches are covered: with
    no handler the default body is built here, with one the handler's response
    is, and the same filtered chain has to apply to each.

    Pinned because getting it wrong is silent and green - the excluded
    middleware simply starts running again on error responses, and every
    assertion about the happy path still holds.
    """
    headers = headers_of(await drive(_excluding_app(broad=broad), http_scope(path="/gone")))
    assert headers["x-kept"] == "ran"
    assert "x-dropped" not in headers


# ── regression: a served request is untouched ────────────────────────


@pytest.mark.parametrize("broad", [False, True])
async def test_a_request_inside_the_limit_is_still_served(broad):
    body = b"x" * _LIMIT
    scope = http_scope(
        method="POST",
        path="/echo",
        headers=[(b"content-length", str(len(body)).encode())],
    )
    messages = await drive(_build(broad=broad), scope, body=body)
    assert status_of(messages) == 200
    assert json.loads(body_of(messages)) == {"size": _LIMIT}


# ── the 405's own exception class is registrable ─────────────────────


async def test_the_405_honours_a_handler_on_its_own_class():
    """`MethodNotAllowed` is the obvious registration for this refusal.

    A base `HTTPException` handler already matched, so a user who reaches for
    the specific public class and gets the built-in body instead is looking at
    an asymmetry, not a design.
    """
    app = _405_app()

    @app.exception_handler(MethodNotAllowed)
    async def handler(request: Request, exc: MethodNotAllowed):
        return JSONResponse({"nope": exc.detail}, status_code=405)

    scope = http_scope(method="POST", path="/only")
    sent = await drive(app, scope)
    assert status_of(sent) == 405
    assert json.loads(body_of(sent)) == {"nope": "Method Not Allowed"}


async def test_the_405_still_matches_a_base_exception_handler():
    """The negative: broadening the class must not narrow what already worked."""
    app = _405_app()

    @app.exception_handler(HTTPException)
    async def handler(request: Request, exc: HTTPException):
        return JSONResponse({"broad": exc.status_code}, status_code=exc.status_code)

    sent = await drive(app, http_scope(method="POST", path="/only"))
    assert json.loads(body_of(sent)) == {"broad": 405}


async def test_the_405_default_body_and_allow_header_are_unchanged():
    """The other negative: an app registering no handler sees the same bytes."""
    sent = await drive(_405_app(), http_scope(method="POST", path="/only"))
    assert status_of(sent) == 405
    assert json.loads(body_of(sent)) == {"detail": "Method Not Allowed", "allowed": ["GET"]}
    assert headers_of(sent).get("allow") == "GET"


async def test_the_404_honours_a_handler_on_its_own_class():
    """Same asymmetry as the 405, on the class users register most."""
    app = _405_app()

    @app.exception_handler(NotFound)
    async def handler(request: Request, exc: NotFound):
        return JSONResponse({"missing": exc.detail}, status_code=404)

    sent = await drive(app, http_scope(method="GET", path="/absent"))
    assert status_of(sent) == 404
    assert json.loads(body_of(sent)) == {"missing": "Not Found"}


async def test_the_404_still_matches_a_base_exception_handler():
    app = _405_app()

    @app.exception_handler(HTTPException)
    async def handler(request: Request, exc: HTTPException):
        return JSONResponse({"broad": exc.status_code}, status_code=exc.status_code)

    sent = await drive(app, http_scope(method="GET", path="/absent"))
    assert json.loads(body_of(sent)) == {"broad": 404}


async def test_the_404_default_body_is_unchanged():
    sent = await drive(_405_app(), http_scope(method="GET", path="/absent"))
    assert status_of(sent) == 404
    assert json.loads(body_of(sent)) == {"detail": "Not Found", "status_code": 404}


# ── every built-in refusal honours its own class ─────────────────────


async def test_the_413_honours_a_handler_on_its_own_class():
    app = Veloce()
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT

    @app.post("/u")
    async def upload(request: Request):
        return {"n": len(await request.body())}

    @app.exception_handler(RequestEntityTooLarge)
    async def handler(request: Request, exc: RequestEntityTooLarge):
        return JSONResponse({"cls": "RequestEntityTooLarge"}, status_code=413)

    sent = await drive(app, http_scope(method="POST", path="/u"), body=_OVER)
    assert status_of(sent) == 413
    assert json.loads(body_of(sent)) == {"cls": "RequestEntityTooLarge"}


async def test_the_429_honours_a_handler_on_its_own_class():
    app = Veloce()
    app.add_middleware(
        RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda request: "one")
    )

    @app.get("/r")
    async def limited(request: Request):
        return {"ok": True}

    @app.exception_handler(TooManyRequests)
    async def handler(request: Request, exc: TooManyRequests):
        return JSONResponse({"cls": "TooManyRequests"}, status_code=429)

    await drive(app, http_scope(method="GET", path="/r"))
    sent = await drive(app, http_scope(method="GET", path="/r"))
    assert status_of(sent) == 429
    assert json.loads(body_of(sent)) == {"cls": "TooManyRequests"}
