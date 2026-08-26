"""One ASGI scope builder and one message-capturing driver, for the tests.

A raw-ASGI drive - the scope dict, a `receive` that yields the body, a `send`
that appends to a list - was re-derived in module after module, and two copies
(`test_gzip_asgi_content_length.py` and `test_gzip_streaming.py`) were
byte-identical for twenty-four lines. Each copy is a chance to omit a key the
app reads, and an omission shows up as a `KeyError` from inside the framework
rather than as a failed assertion, which is a slow thing to debug.

`TestClient` covers most cases and should be preferred. What it deliberately
does not give you is the raw message stream: how many `http.response.body`
messages were emitted, whether `more_body` was set, what the header list looked
like before it was parsed. That is what these are for.

Not every hand-rolled driver belongs here. Several exist to do something
particular - run the request in its own task so no contextvar leaks, feed a body
in numbered chunks, drive an MCP session - and those stay where they are.
"""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.3"},
    "http_version": "1.1",
    "method": "GET",
    "path": "/",
    "raw_path": b"/",
    "query_string": b"",
    "root_path": "",
    "headers": [],
    "scheme": "http",
    "client": ("127.0.0.1", 12345),
    "server": ("testserver", 80),
}


def http_scope(**overrides: Any) -> dict[str, Any]:
    """A complete HTTP scope, with `overrides` applied.

    Every key an ASGI app may read is present, so a test that overrides only
    what it cares about cannot fail on a missing one. `raw_path` follows `path`
    unless given explicitly - they disagreeing is its own class of bug.
    """
    scope = dict(_DEFAULTS)
    scope.update(overrides)
    if "raw_path" not in overrides and "path" in overrides:
        scope["raw_path"] = str(overrides["path"]).encode()
    return scope


async def drive(
    app: Any,
    scope: dict[str, Any] | None = None,
    *,
    body: bytes = b"",
    chunks: list[bytes] | None = None,
    **scope_overrides: Any,
) -> list[dict[str, Any]]:
    """Run one request through `app` and return the messages it sent.

    Pass a `scope`, or the overrides to build one. `body` sends a single request
    message; `chunks` sends one per element with `more_body` set on all but the
    last, which is how a streaming request is fed.
    """
    if scope is None:
        scope = http_scope(**scope_overrides)
    elif scope_overrides:
        scope = {**scope, **scope_overrides}

    pending = list(chunks) if chunks is not None else [body]
    sent = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent < len(pending):
            chunk = pending[sent]
            sent += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": sent < len(pending),
            }
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


def status_of(messages: list[dict[str, Any]]) -> int:
    """The status from the `http.response.start` message."""
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


def headers_of(messages: list[dict[str, Any]]) -> dict[str, str]:
    """The response headers, lowercased, from `http.response.start`."""
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def body_of(messages: list[dict[str, Any]]) -> bytes:
    """Every `http.response.body` payload, concatenated."""
    return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
