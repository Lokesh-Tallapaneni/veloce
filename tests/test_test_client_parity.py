"""The sync and async test clients accept and send the same things.

`TestClient` and `AsyncTestClient` present the same verb shortcuts, and each
funnels its body-carrying verbs through a private helper. The two helpers had
drifted into **different parameter orders** for the same conceptual arguments:

    TestClient._json_or_form(method, path, json, data, headers, content, files, ...)
    AsyncTestClient._dispatch_body(method, path, json, data, content, files, headers, ...)

and all eight call sites passed positionally. Each was self-consistent, so
nothing was wrong on the wire — but `headers` and `content` sat in swapped slots,
so copying a call from one client to the other, or reordering one helper's
parameters, silently swapped a header dict for a request body.

Every call site now passes by keyword, which removes the failure mode rather
than documenting it. These tests hold the two clients to the same behaviour and
the same signatures, so a future divergence fails here.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from veloce import Veloce
from veloce.testclient import AsyncTestClient, TestClient

_BODY_VERBS = ("post", "put", "patch")
_ALL_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    async def echo(request):
        return {
            "method": request.method,
            "body": (await request.body()).decode(),
            "probe": request.headers.get("x-probe"),
            "query": request.query_string,
        }

    for verb in _ALL_VERBS:
        getattr(app, verb)("/e")(echo)
    return app


def _sync(verb, **kwargs):
    return getattr(TestClient(_app()), verb)("/e", **kwargs)


def _async(verb, **kwargs):
    async def run():
        async with AsyncTestClient(_app()) as client:
            return await getattr(client, verb)("/e", **kwargs)

    return asyncio.run(run())


def _compare(verb, **kwargs):
    """Both clients' parsed bodies, with the multipart boundary normalised.

    The boundary is random per request, so two correct clients differ there and
    nowhere else - masking it is what lets the rest be compared exactly.
    """

    def normalise(payload):
        payload = dict(payload)
        payload["body"] = re.sub(r"veloce-[0-9a-f]+", "veloce-BOUNDARY", payload["body"])
        return payload

    return normalise(_sync(verb, **kwargs).json()), normalise(_async(verb, **kwargs).json())


# ── the same call means the same thing on both ───────────────────────


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_a_json_body_is_sent_identically(verb):
    a, b = _compare(verb, json={"a": 1})
    assert a == b


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_raw_content_is_sent_identically(verb):
    a, b = _compare(verb, content=b"raw-bytes")
    assert a == b


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_form_data_is_sent_identically(verb):
    a, b = _compare(verb, data={"field": "value"})
    assert a == b


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_files_are_sent_identically(verb):
    a, b = _compare(verb, files={"f": ("n.txt", b"data", "text/plain")})
    assert a == b


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_headers_are_sent_identically(verb):
    """The pair that sat in swapped positional slots."""
    a, b = _compare(verb, content=b"x", headers={"X-Probe": "p"})
    assert a == b
    assert a["probe"] == "p"


@pytest.mark.parametrize("verb", _BODY_VERBS)
def test_headers_and_content_are_not_transposed(verb):
    """The failure the swapped slots would have produced, named directly."""
    payload, _ = _compare(verb, content=b"the-body", headers={"X-Probe": "the-header"})
    assert payload["body"] == "the-body"
    assert payload["probe"] == "the-header"


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_the_method_reaches_the_handler_on_both(verb):
    sync_resp = _sync(verb)
    async_resp = _async(verb)
    assert sync_resp.status_code == async_resp.status_code


def test_query_parameters_are_sent_identically():
    a, b = _compare("get", params={"q": "hi", "n": "2"})
    assert a == b
    assert "q=hi" in a["query"]


# ── the signatures match ─────────────────────────────────────────────


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_both_clients_declare_the_same_parameters(verb):
    sync_params = list(inspect.signature(getattr(TestClient, verb)).parameters)
    async_params = list(inspect.signature(getattr(AsyncTestClient, verb)).parameters)
    assert sync_params == async_params, verb


def test_both_clients_offer_the_same_verbs():
    def verbs(cls):
        return {
            name
            for name in dir(cls)
            if not name.startswith("_") and callable(getattr(cls, name, None))
        }

    only_sync = verbs(TestClient) - verbs(AsyncTestClient)
    only_async = verbs(AsyncTestClient) - verbs(TestClient)
    #: Sync-only members, each with its reason.
    #:
    #: `close` is the sync lifecycle; the async client uses `async with` and
    #: `__aexit__`. `websocket_connect` is a synchronous context manager by
    #: design.
    #:
    #: `session_transaction` is neither: it is a real asymmetry in the public
    #: surface - a helper the sync client offers and the async one does not,
    #: with no async spelling of it. Exempted here because closing it is a
    #: feature, not this parity fix, and named rather than quietly folded into
    #: the list so it is not lost.
    expected_sync_only = {"close", "websocket_connect", "session_transaction"}
    assert only_sync <= expected_sync_only, only_sync
    assert only_async == set(), only_async


# ── no call site passes the body helpers positionally ────────────────


def test_the_body_helper_is_called_by_keyword():
    """The structural half: positional passing is what allowed the drift.

    The helper used to be `_json_or_form` on one client and `_dispatch_body` on
    the other, taking the same nine arguments in *different* orders. It is one
    name and one order now, and keyword passing keeps the order irrelevant -
    stronger than aligning two orders and hoping they stay aligned.
    """
    source = inspect.getsource(inspect.getmodule(TestClient))
    calls = _call_bodies(source, "self._dispatch_body(")
    assert calls, "no `_dispatch_body` call found - has the helper been renamed?"
    for call in calls:
        assert "json=json" in call, f"called positionally: {call!r}"
        assert "headers=headers" in call, f"called positionally: {call!r}"
        assert "content=content" in call, f"called positionally: {call!r}"


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_both_clients_order_their_parameters_the_same(verb):
    """Names matching is not enough: positional order is part of the contract,
    since `client.get(path, headers)` is a supported call."""

    def positional(cls):
        return [
            p.name
            for p in inspect.signature(getattr(cls, verb)).parameters.values()
            if p.kind is p.POSITIONAL_OR_KEYWORD
        ]

    assert positional(TestClient) == positional(AsyncTestClient), verb


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_both_clients_use_the_same_defaults(verb):
    def defaults(cls):
        return {
            p.name: p.default for p in inspect.signature(getattr(cls, verb)).parameters.values()
        }

    assert defaults(TestClient) == defaults(AsyncTestClient), verb


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_only_the_awaitability_differs(verb):
    """The one difference that is supposed to exist."""
    assert not inspect.iscoroutinefunction(getattr(TestClient, verb))
    assert inspect.iscoroutinefunction(getattr(AsyncTestClient, verb))


def test_both_clients_dispatch_a_body_through_the_same_helper_name():
    assert hasattr(TestClient, "_dispatch_body")
    assert hasattr(AsyncTestClient, "_dispatch_body")
    assert list(inspect.signature(TestClient._dispatch_body).parameters) == list(
        inspect.signature(AsyncTestClient._dispatch_body).parameters
    )


def test_neither_client_wraps_the_shared_body_assembler():
    """The async client had a one-line `_assemble_body` method that only called
    the module function the sync client used directly."""
    assert not hasattr(TestClient, "_assemble_body")
    assert not hasattr(AsyncTestClient, "_assemble_body")


def test_a_signature_difference_would_be_caught():
    """The vacuity guard for the three comparisons above: point one at two
    things known to differ."""
    assert list(inspect.signature(TestClient.get).parameters) != list(
        inspect.signature(TestClient.post).parameters
    )


def _call_bodies(source: str, needle: str) -> list[str]:
    """The argument text of each `needle` call, by brace matching."""
    out = []
    start = source.find(needle)
    while start != -1:
        pos = start + len(needle)
        depth = 1
        while depth:
            if source[pos] == "(":
                depth += 1
            elif source[pos] == ")":
                depth -= 1
            pos += 1
        out.append(source[start + len(needle) : pos - 1])
        start = source.find(needle, pos)
    assert out, needle
    return out
