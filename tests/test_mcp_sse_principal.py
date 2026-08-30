"""On the SSE transport, a tool runs as whoever authenticated the POST.

Both MCP transports authenticate an incoming POST with the same `_authenticate`
helper. The Streamable HTTP one then published the result with `set_principal`,
so the dispatched tool read the caller's identity through `current_principal()`.
The legacy SSE one bound it to `_principal` and threw it away.

Nothing downstream noticed, because the dispatch falls back:
`current_principal() or connection.principal` — and `connection.principal` is
the identity that opened the long-lived **GET stream**. So a POST carrying
Bob's token was validated, accepted with a 202, and then executed as Alice.

That is privilege confusion rather than a missing check: the scopes that ran
were not the scopes that were authorised. Anything tenant- or role-scoped was
affected, on that transport only. `set_principal` was even imported in the
module — just never called on the POST path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import urllib.parse
from collections.abc import AsyncIterator

import pytest

from tests._mcp import SSEStream, auth
from veloce import AsyncTestClient, MCPContext, Veloce
from veloce.contrib.mcp import MCPAuth
from veloce.principal import Principal, current_principal

#: token -> (subject, scopes). Two callers so the two identities are distinct.
TOKENS = {
    "tok-alice": ("alice", ("read",)),
    "tok-bob": ("bob", ("read", "write")),
}


async def _post(app: Veloce, path: str, payload: dict, headers: list) -> int:
    """Send one JSON-RPC POST and return its status code."""
    body = json.dumps(payload).encode()
    status: dict = {}
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    parsed = urllib.parse.urlparse(path)
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": parsed.path,
            "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(),
            "headers": [*headers, (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 5556),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "root_path": "",
        },
        receive,
        send,
    )
    return status.get("code", 0)


def _verify(token: str) -> Principal | None:
    """Map a bearer token to a principal, or refuse it."""
    entry = TOKENS.get(token)
    if entry is None:
        return None
    subject, scopes = entry
    return Principal(subject=subject, scopes=scopes)


def _auth() -> MCPAuth:
    return auth(_verify)


def _app(**mount: object) -> Veloce:
    app = Veloce(title="SSEAuth", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Report who is calling")
    async def whoami() -> dict:
        principal = current_principal()
        return {"subject": None if principal is None else principal.subject}

    @app.mcp_tool(description="Report who is calling, through the context")
    async def whoami_ctx(ctx: MCPContext) -> dict:
        principal = current_principal()
        return {"subject": None if principal is None else principal.subject}

    app.mount_mcp(transport="sse", **mount)
    return app


def _bearer(token: str) -> list:
    return [(b"authorization", f"Bearer {token}".encode())]


def _call(ident: int, name: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name}}


def _subject_of(response: dict) -> str | None:
    return json.loads(response["result"]["content"][0]["text"])["subject"]


@contextlib.asynccontextmanager
async def _open(app: Veloce, token: str | None) -> AsyncIterator[tuple[SSEStream, str]]:
    """Open a stream and yield it with the POST path it advertised.

    A context manager rather than a bare opener: `SSEStream` documents
    `async with` as its usage, and nine tests here drove `__aenter__` /
    `__aexit__` by hand around four lines of try/finally each.
    """
    async with SSEStream(app, headers=_bearer(token) if token else None) as stream:
        frame = await stream.event()
        assert frame["event"] == "endpoint"
        yield stream, frame["data"]


# ── the privilege confusion ──────────────────────────────────────────


async def test_the_tool_runs_as_the_poster_not_the_stream_opener():
    """The defect, exactly as reported: Bob's POST executed as Alice."""
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        assert await _post(app, endpoint, _call(1, "whoami"), _bearer("tok-bob")) == 202
        assert _subject_of(await stream.message()) == "bob"


async def test_the_same_identity_on_both_halves_still_works():
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        assert await _post(app, endpoint, _call(1, "whoami"), _bearer("tok-alice")) == 202
        assert _subject_of(await stream.message()) == "alice"


async def test_two_posts_from_different_callers_each_run_as_themselves():
    """One stream, two callers: neither may inherit the other's identity."""
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        await _post(app, endpoint, _call(1, "whoami"), _bearer("tok-alice"))
        await _post(app, endpoint, _call(2, "whoami"), _bearer("tok-bob"))
        subjects = {}
        for _ in range(2):
            response = await stream.message()
            subjects[response["id"]] = _subject_of(response)
        assert subjects == {1: "alice", 2: "bob"}


async def test_the_identity_reaches_a_tool_taking_a_context():
    """The context path is a separate call shape; it must see the same identity."""
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        await _post(app, endpoint, _call(1, "whoami_ctx"), _bearer("tok-bob"))
        assert _subject_of(await stream.message()) == "bob"


# ── an unauthenticated POST is still refused ─────────────────────────


async def test_a_post_with_no_credentials_is_refused():
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        assert await _post(app, endpoint, _call(1, "whoami"), []) != 202


async def test_a_post_with_an_unknown_token_is_refused():
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        assert await _post(app, endpoint, _call(1, "whoami"), _bearer("tok-nobody")) != 202


async def test_a_refused_post_does_not_run_the_tool():
    """The clearest statement of the property: no token, no execution."""
    app = _app(auth=_auth())
    async with _open(app, "tok-alice") as (stream, endpoint):
        await _post(app, endpoint, _call(1, "whoami"), [])
        with pytest.raises(asyncio.TimeoutError):
            await stream.message(timeout=0.4)


# ── the transport is unchanged without auth ──────────────────────────


async def test_no_auth_leaves_the_principal_unset():
    app = _app()
    async with _open(app, None) as (stream, endpoint):
        assert await _post(app, endpoint, _call(1, "whoami"), []) == 202
        assert _subject_of(await stream.message()) is None


async def test_no_auth_ignores_a_bearer_header():
    """Without an `auth=`, a token is just a header; it must not become identity."""
    app = _app()
    async with _open(app, None) as (stream, endpoint):
        await _post(app, endpoint, _call(1, "whoami"), _bearer("tok-bob"))
        assert _subject_of(await stream.message()) is None


# ── the two transports agree ─────────────────────────────────────────


async def test_the_http_transport_reports_the_same_subject():
    """The comparison that found this: the two must not disagree."""
    app = Veloce(title="HTTPAuth", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Report who is calling")
    async def whoami() -> dict:
        principal = current_principal()
        return {"subject": None if principal is None else principal.subject}

    app.mount_mcp(transport="http", path="/mcp", auth=_auth())

    async with AsyncTestClient(app) as client:
        response = await client.post(
            "/mcp",
            json=_call(1, "whoami"),
            headers={"Authorization": "Bearer tok-bob", "Accept": "application/json"},
        )
        assert _subject_of(response.json()) == "bob"


def test_the_sse_post_path_publishes_the_principal():
    """A guard: the discard-name bug is invisible at runtime until it matters."""
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "veloce"
        / "contrib"
        / "mcp"
        / "transports"
        / "sse.py"
    ).read_text(encoding="utf-8")
    receive = source[source.index("async def receive_message") :]
    assert "_principal, challenge" not in receive
    assert "set_principal(principal)" in receive
