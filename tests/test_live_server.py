"""`LiveServer` — the app over a real loopback socket on the native transport.

Every test here binds an ephemeral port and serves it with
`veloce.serving.protocol.HttpProtocol`, the protocol `Veloce.run()` wires up, so
the HTTP/1.1 parser, keep-alive reuse, chunked framing and the pre-dispatch
refusals are exercised on the wire rather than simulated. The in-memory
`TestClient` cannot reach any of that - it builds ASGI scopes and calls the app
directly - which is the gap this helper exists to close.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import time

import pytest

from veloce import Body, LiveServer, StreamingResponse, Veloce
from veloce.liveserver import _bind_listener
from veloce.serving.protocol import HttpProtocol


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items():
        return {"items": [1, 2, 3]}

    @app.get("/items/{item_id}")
    async def item(item_id: int, q: str = ""):
        return {"item_id": item_id, "q": q}

    @app.post("/echo")
    async def echo(payload: dict = Body()):
        return {"echoed": payload}

    @app.get("/headers")
    async def headers():
        return {"ok": True}, 201, {"X-Custom": "live"}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("handler exploded")

    @app.post("/length")
    async def length(request):
        body = await request.body()
        return {"length": len(body), "body": body.decode()}

    @app.get("/stream")
    async def stream():
        async def produce():
            for part in ("alpha", "beta", "gamma"):
                yield part

        return StreamingResponse(produce(), content_type="text/plain")

    return app


# ── Positive: the ordinary request surface ──────────────────────────


async def test_get_over_a_real_socket_returns_the_body():
    async with LiveServer(_app()) as server:
        response = await server.get("/items")

    assert response.status_code == 200
    assert response.json() == {"items": [1, 2, 3]}
    assert response.headers["content-type"].startswith("application/json")


async def test_port_and_base_url_report_the_bound_socket():
    async with LiveServer(_app()) as server:
        assert server.port != 0
        assert server.base_url == f"http://127.0.0.1:{server.port}"
        assert (await server.get("/items")).status_code == 200


async def test_port_is_unavailable_before_the_server_starts():
    server = LiveServer(_app())
    with pytest.raises(RuntimeError, match="only inside"):
        _ = server.port


async def test_post_round_trips_a_json_body():
    async with LiveServer(_app()) as server:
        response = await server.post("/echo", json={"name": "veloce", "n": 7})

    assert response.status_code == 200
    assert response.json() == {"echoed": {"name": "veloce", "n": 7}}


async def test_path_and_query_parameters_both_resolve():
    async with LiveServer(_app()) as server:
        response = await server.get("/items/42", params={"q": "hello world"})

    assert response.json() == {"item_id": 42, "q": "hello world"}


async def test_response_headers_arrive_over_the_wire():
    async with LiveServer(_app()) as server:
        response = await server.get("/headers")

    assert response.status_code == 201
    assert response.headers["X-Custom"] == "live"


async def test_request_takes_an_arbitrary_verb():
    app = Veloce(openapi_url=None)

    @app.route("/thing", methods=["PUT"])
    async def put_thing(request):
        return {"body": (await request.body()).decode()}

    async with LiveServer(app) as server:
        response = await server.request("put", "/thing", content=b"replaced")

    assert response.json() == {"body": "replaced"}


# ── Positive: what the in-memory client cannot test ─────────────────


async def test_keep_alive_reuses_one_connection_for_two_requests():
    """Two requests, one socket - the in-memory client has no connection at all."""
    async with LiveServer(_app()) as server, server.connect() as connection:
        first = await connection.get("/items/1")
        local = connection._writer.get_extra_info("sockname")
        second = await connection.get("/items/2")

        assert first.json() == {"item_id": 1, "q": ""}
        assert second.json() == {"item_id": 2, "q": ""}
        # Same local address after the second answer: the server kept the
        # connection open rather than closing and being reconnected to.
        assert connection._writer.get_extra_info("sockname") == local
        assert first.headers.get("connection", "").lower() != "close"


async def test_one_socket_serves_a_whole_sequence_of_requests():
    async with LiveServer(_app()) as server, server.connect() as connection:
        local_ports = set()
        for index in range(5):
            response = await connection.get(f"/items/{index}")
            assert response.json()["item_id"] == index
            local_ports.add(connection._writer.get_extra_info("sockname"))
        assert len(local_ports) == 1


async def test_chunked_request_body_is_reassembled_by_the_parser():
    async with LiveServer(_app()) as server:
        response = await server.post("/length", stream=[b"abc", b"de", b"fghij"])

    assert response.json() == {"length": 10, "body": "abcdefghij"}


async def test_streamed_response_is_framed_as_chunked_and_reassembles():
    async with LiveServer(_app()) as server:
        response = await server.get("/stream")

    # `_read_chunked_body` reads each size line and then exactly that many
    # bytes, so a reassembled body is proof the server's chunk framing is
    # byte-correct - a miscounted chunk desynchronises instead.
    assert response.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in response.headers
    assert response.text == "alphabetagamma"


async def test_head_response_carries_headers_and_no_body():
    async with LiveServer(_app()) as server, server.connect() as connection:
        response = await connection.request("HEAD", "/items", close=True)

    assert response.status_code == 200
    assert response.body == b""
    assert response.headers["content-length"] != "0"


# ── Negative: refusals and failures reach the client ────────────────


async def test_unknown_path_is_a_404():
    async with LiveServer(_app()) as server:
        response = await server.get("/nope")

    assert response.status_code == 404


async def test_raising_handler_is_reported_as_a_500_not_a_hang():
    async with LiveServer(_app()) as server:
        response = await server.get("/boom")

    assert response.status_code == 500


async def test_malformed_request_line_is_refused_with_a_400():
    async with LiveServer(_app()) as server, server.connect() as connection:
        await connection.send_raw(b"NOT-A-REQUEST-LINE\r\n\r\n")
        response = await connection.read_response()

    assert response.status_code == 400
    assert response.headers["connection"].lower() == "close"


async def test_body_over_max_content_length_is_refused_with_a_413():
    app = _app()
    app.config["MAX_CONTENT_LENGTH"] = 64

    async with LiveServer(app) as server, server.connect() as connection:
        # The declared length alone is refused at headers-complete, so the
        # oversized body is never put on the wire.
        await connection.send_raw(
            b"POST /length HTTP/1.1\r\n"
            b"Host: %s\r\n"
            b"Content-Length: 1048576\r\n"
            b"Content-Type: application/json\r\n\r\n" % f"{server.host}:{server.port}".encode()
        )
        response = await connection.read_response()

    assert response.status_code == 413


async def test_oversized_request_line_is_refused_with_a_414():
    async with LiveServer(_app()) as server, server.connect() as connection:
        await connection.send_raw(
            b"GET /" + b"x" * 100_000 + b" HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        response = await connection.read_response()

    # The target crosses `MAX_URL_SIZE` while the request line is still being
    # read, so it is refused there rather than routed truncated.
    assert response.status_code == 414


async def test_requesting_on_an_unopened_connection_raises():
    async with LiveServer(_app()) as server:
        connection = server.connect()
        with pytest.raises(RuntimeError, match="not open"):
            await connection.get("/items")


async def test_connect_before_start_raises():
    server = LiveServer(_app())
    with pytest.raises(RuntimeError, match="only inside"):
        server.connect()


# ── Lifecycle ───────────────────────────────────────────────────────


async def test_startup_and_shutdown_hooks_run_around_serving():
    app = _app()
    events: list[str] = []

    @app.on_startup
    async def started():
        events.append("startup")

    @app.on_shutdown
    async def stopped():
        events.append("shutdown")

    async with LiveServer(app) as server:
        assert events == ["startup"]
        assert (await server.get("/items")).status_code == 200

    assert events == ["startup", "shutdown"]


async def test_the_port_is_released_once_the_server_shuts_down():
    async with LiveServer(_app()) as server:
        port = server.port
        assert (await server.get("/items")).status_code == 200

    # Binding the same port again is the observable proof the listener is gone;
    # it would raise `OSError` while the socket was still held.
    rebind = socket.socket()
    try:
        rebind.bind(("127.0.0.1", port))
    finally:
        rebind.close()


async def test_shutdown_leaves_no_connection_or_dispatch_task_behind():
    async with LiveServer(_app()) as server, server.connect() as connection:
        assert (await connection.get("/items")).status_code == 200

    # The protocol tracks every live connection and every in-flight dispatch
    # task on the class, so these two being empty is the framework's own answer
    # to whether anything outlived the server.
    assert list(HttpProtocol._live_connections) == []
    assert HttpProtocol._active_tasks == set()
    assert server._connections == set()


async def test_a_failing_startup_hook_releases_the_listening_socket():
    app = _app()

    @app.on_startup
    async def explode():
        raise RuntimeError("startup failed")

    server = LiveServer(app)
    with pytest.raises(RuntimeError, match="startup failed"):
        await server.__aenter__()

    # `__aexit__` never runs for a context manager whose entry raised, so the
    # bind must be undone on the way out of `__aenter__` itself.
    assert server._server is None


async def test_two_live_servers_get_different_ports():
    async with LiveServer(_app()) as first, LiveServer(_app()) as second:
        assert first.port != second.port
        assert (await first.get("/items")).status_code == 200
        assert (await second.get("/items")).status_code == 200


# ── Teardown is bounded ─────────────────────────────────────────────
#
# `__aexit__` closed the connections it had opened itself and then awaited
# `wait_closed()` with no bound. On Python 3.12 that waits for *every* live
# connection, so a test driving the server with an external client - httpx,
# requests, a raw socket - over keep-alive blocked teardown for as long as the
# client held the socket, which is to say until the suite was killed.


async def _untracked_connection(server: LiveServer) -> tuple:
    """A client socket the server did not open, left open on purpose."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    crlf = bytes((13, 10))
    writer.write(
        b"GET /items HTTP/1.1" + crlf + b"Host: t" + crlf + b"Connection: keep-alive" + crlf + crlf
    )
    await writer.drain()
    first = await asyncio.wait_for(reader.read(1024), timeout=5)
    assert first.startswith(b"HTTP/1.1 200"), first
    return reader, writer


async def test_teardown_completes_with_an_untracked_connection_still_open():
    server = LiveServer(_app(), shutdown_timeout=0.5)
    await server.__aenter__()
    _reader, writer = await _untracked_connection(server)
    try:
        started = time.monotonic()
        await asyncio.wait_for(server.__aexit__(), timeout=10)
        # Immediate, not merely bounded: `begin_drain` closes an idle
        # keep-alive socket at once, so sitting out `shutdown_timeout` here
        # would be the regression.
        assert time.monotonic() - started < 1
    finally:
        writer.close()


async def test_the_port_is_released_even_though_a_client_held_on():
    """The listener has to be gone whether or not the clients cooperated."""
    server = LiveServer(_app(), shutdown_timeout=0.5)
    await server.__aenter__()
    port = server.port
    _reader, writer = await _untracked_connection(server)
    try:
        await asyncio.wait_for(server.__aexit__(), timeout=10)
    finally:
        writer.close()

    rebind = socket.socket()
    try:
        rebind.bind(("127.0.0.1", port))
    finally:
        rebind.close()


async def test_an_idle_connection_is_drained_rather_than_waited_out():
    """With the default bound: an idle keep-alive socket costs no time at all."""
    server = LiveServer(_app())
    await server.__aenter__()
    _reader, writer = await _untracked_connection(server)
    try:
        started = time.monotonic()
        await asyncio.wait_for(server.__aexit__(), timeout=30)
        assert time.monotonic() - started < 1
    finally:
        writer.close()


async def test_the_shutdown_bound_is_configurable():
    server = LiveServer(_app(), shutdown_timeout=0.1)
    await server.__aenter__()
    _reader, writer = await _untracked_connection(server)
    try:
        started = time.monotonic()
        await asyncio.wait_for(server.__aexit__(), timeout=10)
        elapsed = time.monotonic() - started
    finally:
        writer.close()
    # Bounded by the value given, not by the client.
    assert elapsed < 3


async def test_a_graceful_teardown_is_not_delayed_by_the_bound():
    """The negative: with nothing holding on, teardown must not wait at all."""
    server = LiveServer(_app(), shutdown_timeout=30)
    await server.__aenter__()
    async with server.connect() as connection:
        assert (await connection.get("/items")).status_code == 200
    started = time.monotonic()
    await server.__aexit__()
    assert time.monotonic() - started < 5


async def test_teardown_does_not_disturb_another_live_server():
    """Forcing one server's stragglers must not reach into a second one."""
    first = LiveServer(_app(), shutdown_timeout=0.2)
    second = LiveServer(_app())
    await first.__aenter__()
    await second.__aenter__()
    try:
        _reader, writer = await _untracked_connection(first)
        held = await _untracked_connection(second)
        try:
            await asyncio.wait_for(first.__aexit__(), timeout=10)
            # The second server is untouched and still serving on its own socket.
            assert (await second.get("/items")).status_code == 200
        finally:
            writer.close()
            held[1].close()
    finally:
        await asyncio.wait_for(second.__aexit__(), timeout=10)


# ── the base URL names a host a client can use ───────────────────────


async def test_an_ipv6_host_is_bracketed_in_the_host_header_too():
    """The app parses `Host:`; `::1:8080` is not an authority it can read.

    Unbracketed, `request.url` fell back to a default host, so a test asserting
    on the URL the app saw was testing the fallback.
    """
    app = _app()

    @app.get("/where")
    async def where(request):
        return {"host": request.headers.get("host"), "url": str(request.url)}

    try:
        async with LiveServer(app, host="::1") as server:
            body = (await server.get("/where")).json()
            assert body["host"] == f"[::1]:{server.port}"
            assert body["url"] == f"http://[::1]:{server.port}/where"
    except OSError as exc:  # pragma: no cover - a host without IPv6 loopback
        pytest.skip(f"no IPv6 loopback here: {exc}")


async def test_a_name_resolving_to_both_families_binds_one_socket():
    """`localhost` bound two sockets, and with port 0 each got a different port.

    Only the first was reported, so a client resolving the name to the other
    family connected to a port nothing was listening on.
    """
    async with LiveServer(_app(), host="localhost") as server:
        assert server._server is not None
        sockets = server._server.sockets
        assert len(sockets) == 1
        assert sockets[0].getsockname()[1] == server.port
        assert (await server.get("/items")).status_code == 200


async def test_the_authority_is_what_the_base_url_and_host_header_share():
    async with LiveServer(_app(), host="127.0.0.1") as server:
        assert server.authority == f"127.0.0.1:{server.port}"
        assert server.base_url == f"http://{server.authority}"


async def test_an_ipv6_host_is_bracketed_in_the_base_url():
    """`http://::1:8080` runs the address into the port (RFC 3986 Sec. 3.2.2)."""
    try:
        async with LiveServer(_app(), host="::1") as server:
            assert server.base_url == f"http://[::1]:{server.port}"
    except OSError as exc:  # pragma: no cover - a host without IPv6 loopback
        pytest.skip(f"no IPv6 loopback here: {exc}")


async def test_an_ipv4_host_is_not_bracketed():
    async with LiveServer(_app(), host="127.0.0.1") as server:
        assert server.base_url == f"http://127.0.0.1:{server.port}"


async def test_a_named_host_reports_the_address_it_bound():
    """A name can resolve to several addresses and only one is listening.

    Reporting the name built a `base_url` that may not connect, and a `Host:`
    header naming a host the app could not match. The bound address is the one
    thing that is certainly reachable.
    """
    async with LiveServer(_app(), host="localhost") as server:
        assert server.host == "localhost"
        assert server.base_url == f"http://{server.authority}"
        assert "localhost" not in server.authority
        assert (await server.get("/items")).status_code == 200


# ── teardown finishes even when a phase of it fails ──────────────────
#
# `__aexit__` closes the client connections, then the listener, then runs the
# app's shutdown hooks. Run as one straight line, a raise in the first phase
# skipped the other two: the listening socket stayed bound - so the next test
# asking for that port failed - and the app never saw its shutdown.


async def _raise_on_close(server: LiveServer) -> None:
    connection = server.connect()
    await connection.__aenter__()
    assert (await connection.get("/items")).status_code == 200

    async def boom() -> None:
        raise RuntimeError("closing this client connection failed")

    connection.aclose = boom  # type: ignore[method-assign]


async def test_a_failed_client_close_still_releases_the_port():
    server = LiveServer(_app(), shutdown_timeout=0.5)
    await server.__aenter__()
    port = server.port
    await _raise_on_close(server)

    with pytest.raises(RuntimeError, match="closing this client connection failed"):
        await server.__aexit__()

    # Binding it again is the observable proof the listener is gone.
    rebind = socket.socket()
    try:
        rebind.bind(("127.0.0.1", port))
    finally:
        rebind.close()


async def test_a_failed_client_close_still_runs_the_shutdown_hooks():
    app = _app()
    ran: list[str] = []

    @app.on_shutdown
    async def note() -> None:
        ran.append("shutdown")

    server = LiveServer(app, shutdown_timeout=0.5)
    await server.__aenter__()
    await _raise_on_close(server)

    with pytest.raises(RuntimeError):
        await server.__aexit__()
    assert ran == ["shutdown"]


async def test_a_failed_client_close_leaves_no_connection_registered():
    server = LiveServer(_app(), shutdown_timeout=0.5)
    await server.__aenter__()
    await _raise_on_close(server)

    with pytest.raises(RuntimeError):
        await server.__aexit__()
    assert server._connections == set()
    assert list(server._protocols) == []


async def test_a_clean_teardown_still_raises_nothing():
    """The control: the happy path is unchanged by the guards around it."""
    app = _app()
    ran: list[str] = []

    @app.on_shutdown
    async def note() -> None:
        ran.append("shutdown")

    async with LiveServer(app) as server, server.connect() as connection:
        assert (await connection.get("/items")).status_code == 200
    assert ran == ["shutdown"]


# ── a wildcard host is not an address to talk to ─────────────────────
#
# A wildcard is an instruction to bind every interface. Advertising it produced
# a `base_url` no client could use, and connecting to it fails outright on
# Windows - so every request against such a server failed.


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("0.0.0.0", "127.0.0.1"), ("::", "::1")],
    ids=["ipv4-wildcard", "ipv6-wildcard"],
)
async def test_a_wildcard_host_reports_and_uses_its_loopback(host, loopback):
    try:
        async with LiveServer(_app(), host=host, shutdown_timeout=1) as server:
            assert server.host == host
            assert loopback in server.authority
            assert (await server.get("/items")).status_code == 200
    except OSError as exc:  # pragma: no cover - a host without that family
        pytest.skip(f"cannot bind {host}: {exc}")


async def test_an_empty_host_binds_every_interface():
    """Resolved without `AI_PASSIVE` an empty host gave the first real
    interface - a link-local one - where it reads as "all of them". One socket
    can only serve every interface for both families as a dual-stack one:
    v6-only on `::` refused IPv4 outright, so `127.0.0.1` timed out against a
    server reading as "all interfaces"."""
    async with LiveServer(_app(), host="", shutdown_timeout=1) as server:
        assert server._server is not None
        listener = server._server.sockets[0]
        assert listener.getsockname()[0] in ("::", "0.0.0.0")
        assert (await server.get("/items")).status_code == 200
        if listener.family != socket.AF_INET6:  # pragma: no cover
            pytest.skip("no IPv6 wildcard to bind")
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            client = socket.socket(family, socket.SOCK_STREAM)
            client.settimeout(2.0)
            try:
                client.connect((address, server.port))
            finally:
                client.close()


# ── the Host header the caller asked for ─────────────────────────────
#
# The authority is filled in for callers who do not care, so a caller who does
# has to be able to replace it. Merging through a dict matched the name by case:
# a lowercase `host` sat beside it and two `Host` lines went out, which is a
# malformed request (RFC 9110 Sec. 7.2) whose first line the server reads - so
# the header the caller sent was the one ignored.


@pytest.mark.parametrize("name", ["host", "Host", "HOST"], ids=["lower", "title", "upper"])
async def test_a_host_header_replaces_the_authority_whatever_its_case(name):
    app = Veloce()

    @app.get("/seen")
    async def seen(request):
        return {"host": request.headers.getlist("host")}

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.get("/seen", headers={name: "vhost.example"})
    assert response.json() == {"host": ["vhost.example"]}


async def test_the_authority_is_sent_when_the_caller_names_no_host():
    """The negative: nothing to replace, so the server still learns an
    authority that reaches it."""
    app = Veloce()

    @app.get("/seen")
    async def seen(request):
        return {"host": request.headers.getlist("host")}

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.get("/seen")
        assert response.json() == {"host": [server.authority]}


async def test_a_lowercase_framing_header_is_not_sent_twice():
    """The framing this client computes replaces a caller's rather than
    doubling it; `send_raw` is the way to put malformed framing on the wire."""
    app = Veloce()

    @app.post("/body")
    async def body(request):
        return {"lengths": request.headers.getlist("content-length")}

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.post("/body", content=b"abc", headers={"content-length": "999"})
    assert response.json() == {"lengths": ["3"]}


async def test_a_repeated_header_reaches_the_server():
    """Ordered pairs rather than a mapping, so a header HTTP allows twice
    arrives twice - the reason the merge keeps pairs at all."""
    app = Veloce()

    @app.get("/hops")
    async def hops(request):
        return {"hops": request.headers.getlist("x-forwarded-for")}

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.get(
            "/hops",
            headers=[("X-Forwarded-For", "10.0.0.1"), ("X-Forwarded-For", "10.0.0.2")],
        )
    assert response.json() == {"hops": ["10.0.0.1", "10.0.0.2"]}


async def test_a_streamed_body_removes_a_caller_content_length():
    """This client owns the framing, and `Content-Length` beside
    `Transfer-Encoding` is forbidden (RFC 9112 Sec. 6.1) - the native
    transport answers 400."""
    app = Veloce()

    @app.post("/echo", stream=True)
    async def echo(request):
        chunks = [chunk async for chunk in request.stream()]
        return {
            "body": b"".join(chunks).decode(),
            "cl": request.headers.get("content-length"),
            "te": request.headers.get("transfer-encoding"),
        }

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.post(
            "/echo", headers={"Content-Length": "999"}, stream=[b"hello", b"world"]
        )
    assert response.status_code == 200
    assert response.json() == {"body": "helloworld", "cl": None, "te": "chunked"}


async def test_a_buffered_body_removes_a_caller_transfer_encoding():
    """The mirror: a `Transfer-Encoding` left on an unchunked body is read in
    preference to `Content-Length` and desynchronises the connection."""
    app = Veloce()

    @app.post("/echo")
    async def echo(request):
        return {
            "body": (await request.body()).decode(),
            "cl": request.headers.get("content-length"),
            "te": request.headers.get("transfer-encoding"),
        }

    async with LiveServer(app, shutdown_timeout=1) as server:
        response = await server.post(
            "/echo", content=b"abc", headers={"Transfer-Encoding": "chunked"}
        )
    assert response.status_code == 200
    assert response.json() == {"body": "abc", "cl": "3", "te": None}


async def test_a_first_address_that_will_not_bind_falls_through_to_the_next(monkeypatch):
    """A kernel with IPv6 switched off still answers `::` for a passive lookup
    and then refuses to bind it; binding only the first answer made that
    fatal, where asyncio's own server moves on."""
    resolve = socket.getaddrinfo

    def unbindable_first(host, port, *args, **kwargs):
        # TEST-NET-1 (RFC 5737) is not assigned to this host, so the bind fails.
        refused = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]
        return refused + resolve("127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", unbindable_first)
    async with LiveServer(_app(), shutdown_timeout=1) as server:
        assert server.authority.startswith("127.0.0.1:")
        assert (await server.get("/items")).status_code == 200


async def test_binding_raises_when_no_resolved_address_will_take_it(monkeypatch):
    """The negative: the error from the last address attempted, not silence."""

    def all_unbindable(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", all_unbindable)
    with pytest.raises(OSError):
        async with LiveServer(_app(), shutdown_timeout=1):
            pass  # pragma: no cover - the bind above raises


# ── binding walks the resolved addresses ──────────────────────────────
#
# Each of these drives `_bind_listener` directly: the failures are real OS
# refusals of a crafted address list, so nothing has to stand in for the kernel.


def _resolved(host: str, port: int = 0) -> list[tuple[object, ...]]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)


def test_an_address_whose_socket_cannot_be_created_falls_through_to_the_next():
    """A family the kernel has switched off fails at `socket()`, before any
    bind - so creating the socket outside the loop's error handling made the
    very case the loop exists for fatal."""
    unsupported = (socket.AF_INET6, socket.SOCK_STREAM, 999, "", ("::1", 0, 0, 0))
    sock = _bind_listener([unsupported, *_resolved("127.0.0.1")], "")
    try:
        assert sock.family == socket.AF_INET
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_a_bind_failure_that_is_not_about_the_address_is_raised_where_it_happened():
    """A port already in use would fail the same way at every address, so
    retrying hides the error that explains it."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        with pytest.raises(OSError) as caught:
            _bind_listener(_resolved("127.0.0.1", port), "127.0.0.1")
    finally:
        held.close()
    assert caught.value.errno == errno.EADDRINUSE
    assert "cannot bind" in str(caught.value)
    assert str(port) in str(caught.value)


def test_the_bind_error_names_the_host_and_every_address_tried():
    """Only the last error was raised, with no host and no cause attached."""
    # TEST-NET-1 (RFC 5737), neither address assigned to this host.
    unavailable = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 0)),
    ]
    with pytest.raises(OSError) as caught:
        _bind_listener(unavailable, "example.invalid")
    message = str(caught.value)
    assert "example.invalid" in message
    assert "192.0.2.1" in message and "192.0.2.2" in message
    assert isinstance(caught.value.__cause__, OSError)
