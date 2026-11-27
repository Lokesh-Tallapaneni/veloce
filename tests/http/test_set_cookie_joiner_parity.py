"""Every path that splits multiple cookies apart uses one constant.

`Response.set_cookie` stores several cookies in one `Set-Cookie` header entry,
joined by `SET_COOKIE_JOINER`. Three places split them back out to emit one
header per cookie: the ASGI header builder, the native wire encoder, and
`Response.headerlist`.

Two used the constant. `app/asgi.py` used the string literal `"\\r\\nSet-Cookie:"`
— the constant minus its trailing space — which happened to work only because it
is a prefix of the constant and a `.strip()` on each piece hid the difference.
The bug was latent rather than live: changing the constant would have left the
ASGI path splitting on the old separator and emitting several cookies inside one
header, on one transport only.

These tests pin the behaviour on every path and, separately, pin that no path
carries its own copy of the separator.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from tests._asgi_drive import http_scope
from tests._native_client import NativeClient
from veloce import JSONResponse, Veloce
from veloce._internal import _encode_response_head
from veloce._protocol_constants import SET_COOKIE_JOINER
from veloce.app.asgi import _build_asgi_headers
from veloce.http.response import Response

_COOKIES = [("a", "1"), ("b", "2"), ("c", "3")]


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def cookies():
        resp = JSONResponse({"ok": True})
        for name, value in _COOKIES:
            resp.set_cookie(name, value)
        return resp

    return app


def _asgi_set_cookies() -> list[str]:
    captured: list[str] = []

    async def run():
        async def send(message):
            if message["type"] == "http.response.start":
                captured.extend(v.decode() for k, v in message["headers"] if k == b"set-cookie")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await _app()(
            http_scope(
                type="http",
                method="GET",
                path="/c",
                raw_path=b"/c",
                query_string=b"",
                headers=[],
                client=("1.2.3.4", 1),
                scheme="http",
                server=("t", 80),
                http_version="1.1",
                root_path="",
            ),
            receive,
            send,
        )

    asyncio.run(run())
    return captured


def _native_set_cookies() -> list[str]:
    client = NativeClient(_app())
    try:
        raw = client.get("/c").raw.decode("latin-1")
    finally:
        client.close()
    return [
        line.partition(":")[2].strip()
        for line in raw.split("\r\n")
        if line.lower().startswith("set-cookie:")
    ]


def _headerlist_set_cookies() -> list[str]:
    resp = JSONResponse({"ok": True})
    for name, value in _COOKIES:
        resp.set_cookie(name, value)
    return [v for k, v in resp.headerlist if k.lower() == "set-cookie"]


# ── one header per cookie, on every path ─────────────────────────────


@pytest.mark.parametrize(
    ("name", "emit"),
    [
        ("asgi", _asgi_set_cookies),
        ("native", _native_set_cookies),
        ("headerlist", _headerlist_set_cookies),
    ],
)
def test_each_cookie_gets_its_own_header(name, emit):
    assert len(emit()) == len(_COOKIES), name


@pytest.mark.parametrize(
    ("name", "emit"),
    [
        ("asgi", _asgi_set_cookies),
        ("native", _native_set_cookies),
        ("headerlist", _headerlist_set_cookies),
    ],
)
def test_every_cookie_survives(name, emit):
    joined = " ".join(emit())
    for cookie_name, value in _COOKIES:
        assert f"{cookie_name}={value}" in joined, (name, cookie_name)


@pytest.mark.parametrize(
    ("name", "emit"),
    [
        ("asgi", _asgi_set_cookies),
        ("native", _native_set_cookies),
        ("headerlist", _headerlist_set_cookies),
    ],
)
def test_no_header_carries_the_separator(name, emit):
    """A header still containing the joiner is one that failed to split."""
    for header in emit():
        assert "Set-Cookie" not in header, (name, header)


@pytest.mark.parametrize(
    ("name", "emit"),
    [
        ("asgi", _asgi_set_cookies),
        ("native", _native_set_cookies),
        ("headerlist", _headerlist_set_cookies),
    ],
)
def test_no_header_has_leading_whitespace(name, emit):
    """Splitting on a prefix of the separator leaves a space behind."""
    for header in emit():
        assert header == header.lstrip(), (name, header)


def test_the_three_paths_emit_the_same_cookies():
    """Asserted against each other, so one drifting fails here."""
    asgi = _asgi_set_cookies()
    native = _native_set_cookies()
    headerlist = _headerlist_set_cookies()
    assert asgi == native == headerlist


def test_a_single_cookie_is_unaffected():
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def one():
        resp = JSONResponse({"ok": True})
        resp.set_cookie("only", "1")
        return resp

    client = NativeClient(app)
    try:
        assert "only=1" in client.get("/c").headers["set-cookie"]
    finally:
        client.close()


# ── no path keeps its own copy of the separator ──────────────────────


def test_no_splitter_hardcodes_the_separator():
    """The structural half: the literal is what allowed the drift.

    Scoped to the three functions that actually split, rather than to whole
    files - the separator is legitimately *described* in several comments and
    docstrings, and flagging those would make this guard noise.
    """
    for name, func in (
        ("_build_asgi_headers", _build_asgi_headers),
        ("_encode_response_head", _encode_response_head),
        ("Response.headerlist", Response.headerlist.fget),
    ):
        code = inspect.getsource(func)
        # Asserted as a positive - that the split call names the constant -
        # rather than as the absence of a literal: the separator is legitimately
        # described in this function's own docstring, so an absence check would
        # flag prose.
        assert "split(SET_COOKIE_JOINER" in code, f"{name} does not split on the shared constant"


def test_the_separator_is_defined_once():
    assert SET_COOKIE_JOINER.startswith("\r\n")
    assert "Set-Cookie" in SET_COOKIE_JOINER
