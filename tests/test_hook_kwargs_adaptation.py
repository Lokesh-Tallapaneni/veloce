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
    """The property that was broken, asserted at the helpers rather than
    through two separate routes, so a future divergence names itself."""
    import inspect

    from veloce.app.dispatch import DispatchMixin

    after_src = inspect.getsource(DispatchMixin._call_after_hook)
    exc_src = inspect.getsource(DispatchMixin._call_exc_handler)
    assert "VAR_KEYWORD" in after_src
    assert "VAR_KEYWORD" in exc_src, (
        "the exception-handler helper does not check for **kwargs; it will call a "
        "handler declared `def handler(**kwargs)` with an empty dict"
    )


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
