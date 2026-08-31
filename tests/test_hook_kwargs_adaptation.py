"""A hook taking `**kwargs` receives what it is offered, whichever hook it is.

Veloce adapts what it passes a callback to that callback's signature: an
`after_request` hook may declare `request`, `response`, both, or neither, and an
exception handler may declare `request`, `exc`, both, or neither. Two nearly
identical helpers do that adaptation — `_call_after_hook` and
`_call_exc_handler` — and they had drifted:

    @app.after_request
    async def after(**kwargs): ...          # receives request and response

    @app.exception_handler(ValueError)
    async def handle(**kwargs): ...         # receives {} — nothing at all

`_call_after_hook` checks for `VAR_KEYWORD` and offers everything; the exception
handler's copy tested `"request" in params` against a signature that declares
neither name, matched nothing, and called the handler with an empty dict. A
handler written that way could not see the exception it was handling, and
nothing said so.

The helpers are not merged. Merging them would put a shared call on the
per-request after-hook path, and this repository has a measurement showing what
that costs. They are pinned against each other instead, which buys the same
guarantee at no runtime cost — the same trade made for the MCP/HTTP lifecycle
seam.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.app.dispatch import _adapt_hook_kwargs
from veloce.testclient import TestClient

# ── the exception handler ────────────────────────────────────────────


def _exc_app(handler):
    app = Veloce(openapi_url=None)
    app.exception_handler(ValueError)(handler)

    @app.get("/boom")
    async def boom():
        raise ValueError("the-message")

    return TestClient(app)


def test_an_exception_handler_taking_kwargs_receives_both():
    """The defect: this received `{}`."""
    seen = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handler).get("/boom").status_code == 418
    assert sorted(seen) == ["exc", "request"]


def test_an_exception_handler_taking_kwargs_can_read_the_exception():
    """What the empty dict actually cost: the handler was blind."""
    seen = {}

    async def handler(**kwargs):
        seen["message"] = str(kwargs["exc"])
        return Response(body=b"ok", status_code=418)

    _exc_app(handler).get("/boom")
    assert seen["message"] == "the-message"


def test_an_exception_handler_taking_kwargs_can_read_the_request():
    seen = {}

    async def handler(**kwargs):
        seen["path"] = kwargs["request"].path
        return Response(body=b"ok", status_code=418)

    _exc_app(handler).get("/boom")
    assert seen["path"] == "/boom"


def test_an_exception_handler_mixing_a_name_and_kwargs_receives_both():
    seen = {}

    async def handler(request, **kwargs):
        seen["path"] = request.path
        seen["rest"] = sorted(kwargs)
        return Response(body=b"ok", status_code=418)

    _exc_app(handler).get("/boom")
    assert seen["path"] == "/boom"
    assert seen["rest"] == ["exc"]


# ── the named forms, which must not change ───────────────────────────


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        pytest.param(lambda seen: _named_both(seen), ["exc", "request"], id="both"),
        pytest.param(lambda seen: _named_request(seen), ["request"], id="request-only"),
        pytest.param(lambda seen: _named_exc(seen), ["exc"], id="exc-only"),
        pytest.param(lambda seen: _named_none(seen), [], id="neither"),
    ],
)
def test_an_exception_handler_receives_exactly_what_it_names(make, expected):
    seen: dict = {}
    _exc_app(make(seen)).get("/boom")
    assert sorted(seen) == expected


def _named_both(seen):
    async def handler(request, exc):
        seen["request"] = request
        seen["exc"] = exc
        return Response(body=b"ok", status_code=418)

    return handler


def _named_request(seen):
    async def handler(request):
        seen["request"] = request
        return Response(body=b"ok", status_code=418)

    return handler


def _named_exc(seen):
    async def handler(exc):
        seen["exc"] = exc
        return Response(body=b"ok", status_code=418)

    return handler


def _named_none(seen):
    async def handler():
        return Response(body=b"ok", status_code=418)

    return handler


# ── the after-request hook, which was already right ──────────────────


def _after_app(hook):
    app = Veloce(openapi_url=None)
    app.after_request(hook)

    @app.get("/")
    async def index():
        return {"ok": True}

    return TestClient(app)


def test_an_after_request_hook_taking_kwargs_receives_both():
    seen = {}

    async def hook(**kwargs):
        seen.update(kwargs)
        return kwargs["response"]

    assert _after_app(hook).get("/").status_code == 200
    assert sorted(seen) == ["request", "response"]


@pytest.mark.parametrize(
    ("hook_factory", "expected"),
    [
        pytest.param(lambda s: _after_both(s), ["request", "response"], id="both"),
        pytest.param(lambda s: _after_response(s), ["response"], id="response-only"),
    ],
)
def test_an_after_request_hook_receives_exactly_what_it_names(hook_factory, expected):
    seen: dict = {}
    _after_app(hook_factory(seen)).get("/")
    assert sorted(seen) == expected


def _after_both(seen):
    async def hook(request, response):
        seen["request"] = request
        seen["response"] = response
        return response

    return hook


def _after_response(seen):
    async def hook(response):
        seen["response"] = response
        return response

    return hook


# ── the two helpers, pinned against each other ───────────────────────


def test_both_helpers_treat_a_var_keyword_signature_the_same_way():
    """The property that was broken, asserted at the adapter itself.

    This used to match `"VAR_KEYWORD"` in `inspect.getsource` of both methods,
    which pinned the spelling rather than the behaviour - and broke the moment
    the two were consolidated onto one adapter that is demonstrably correct.
    Now it drives that adapter directly, with each caller's configuration, so it
    survives refactoring and fails only on an actual divergence.
    """

    def var_keyword(**kwargs):
        return kwargs

    request, response, exc = object(), object(), ValueError("x")
    after = _adapt_hook_kwargs(var_keyword, {}, "response", request, response)
    handler = _adapt_hook_kwargs(var_keyword, {}, "exc", request, exc)

    assert sorted(after) == ["request", "response"]
    assert sorted(handler) == ["exc", "request"], (
        "a handler declared `def handler(**kwargs)` is called with an empty dict "
        "and cannot see the exception it is handling"
    )


def test_the_adapter_offers_only_what_a_named_signature_asks_for():
    """The other half: a named signature gets exactly its parameters."""

    def wants_both(request, response):
        return None

    def wants_one(response):
        return None

    def wants_none():
        return None

    request, response = object(), object()
    assert sorted(_adapt_hook_kwargs(wants_both, {}, "response", request, response)) == [
        "request",
        "response",
    ]
    assert sorted(_adapt_hook_kwargs(wants_one, {}, "response", request, response)) == ["response"]
    assert _adapt_hook_kwargs(wants_none, {}, "response", request, response) == {}


def test_the_adapter_caches_its_answer():
    """The cache is why the signature is inspected once rather than per request."""

    def hook(request, response):
        return None

    cache: dict = {}
    _adapt_hook_kwargs(hook, cache, "response", object(), object())
    assert cache[hook] == (True, True)
    _adapt_hook_kwargs(hook, cache, "response", object(), object())
    assert len(cache) == 1


def test_an_unhashable_callable_still_adapts():
    """`contextlib.suppress(TypeError)` around the cache write exists for this;
    without it an unhashable callable would raise instead of being adapted."""

    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __call__(self, request, response):
            return None

    kwargs = _adapt_hook_kwargs(Unhashable(), {}, "response", object(), object())
    assert sorted(kwargs) == ["request", "response"]


def test_the_two_helpers_offer_everything_to_a_var_keyword_signature():
    """Behavioural parity: each offers two names, and `**kwargs` gets both."""
    exc_seen, after_seen = {}, {}

    async def exc_handler(**kwargs):
        exc_seen["names"] = sorted(kwargs)
        return Response(body=b"ok", status_code=418)

    async def after_hook(**kwargs):
        after_seen["names"] = sorted(kwargs)
        return kwargs["response"]

    _exc_app(exc_handler).get("/boom")
    _after_app(after_hook).get("/")
    assert exc_seen["names"] == ["exc", "request"]
    assert after_seen["names"] == ["request", "response"]


# ── a hook that cannot be weakly referenced ──────────────────────────
#
# The signature caches are `WeakKeyDictionary`s, so a callable that cannot be
# weakly referenced raises `TypeError` on *lookup*, not only on insert. Only the
# insert was guarded, so registering such a hook answered `500` on every
# request - and the guard on the insert shows uncacheable callables were meant
# to be tolerated all along. An instance of a slotted class is the ordinary way
# to get one; `str.upper` and other method descriptors are the same shape.


class _SlottedHook:
    """A callable that cannot be weakly referenced (no `__weakref__` slot)."""

    __slots__ = ()

    def __call__(self, request, response):
        response.headers["X-Slotted-Hook"] = "ran"
        return response


class _SlottedErrorHandler:
    __slots__ = ()

    def __call__(self, request, exc):
        return Response(body=b"handled", status_code=418)


def test_a_non_weakref_able_after_hook_runs():
    """The defect: this answered 500, from inside the framework's own cache."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    app.after_request(_SlottedHook())
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert resp.headers["X-Slotted-Hook"] == "ran"


def test_a_non_weakref_able_error_handler_runs():
    app = Veloce(openapi_url=None)
    app.register_error_handler(ValueError, _SlottedErrorHandler())

    @app.get("/boom")
    async def boom():
        raise ValueError("x")

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 418
    assert resp.body == b"handled"


def test_a_non_weakref_able_hook_runs_on_every_request():
    """Not cached, so it must keep working rather than working once."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    app.after_request(_SlottedHook())
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/").headers["X-Slotted-Hook"] == "ran"


def test_a_weakref_able_hook_is_still_cached():
    """The negative: tolerating the uncacheable case must not disable caching."""

    def hook(request, response):
        return None

    cache: dict = {}
    _adapt_hook_kwargs(hook, cache, "response", object(), object())
    assert hook in cache
