"""SessionMiddleware — opt-in transparent large-cookie chunking (RFC 6265 §6.1)."""

from __future__ import annotations

import logging

from tests.conftest import make_request
from veloce import Request, Response, Session, SessionMiddleware, TestClient, Veloce


def _req(headers: dict[str, str] | None = None) -> Request:
    return make_request(method="GET", path="/x", query_string="", headers=headers or {}, body=b"")


async def test_chunking_off_by_default_drops_oversized_cookie(caplog):
    """Default (chunked=False): the existing drop-with-warning behavior is
    unchanged — no Set-Cookie, one warning."""
    mw = SessionMiddleware(secret_key="k" * 32)
    request = _req()
    session = Session({"blob": "x" * 8192})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    with caplog.at_level(logging.WARNING, logger="veloce.sessions"):
        result = await mw.process_response(request, response)

    assert not any(k.lower() == "set-cookie" for k in result.headers)
    matches = [r for r in caplog.records if r.name == "veloce.sessions"]
    assert matches and "max_cookie_size" in matches[-1].getMessage()


async def test_oversized_session_splits_into_chunks():
    """chunked=True: a >4KB session is written across numbered cookies, each
    within the size limit, and the base cookie is cleared."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True)
    request = _req()
    session = Session({"blob": "x" * 8192})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    await mw.process_response(request, response)
    raw = response.headers.get("Set-Cookie")
    assert raw is not None
    lines = raw.split("\r\nSet-Cookie: ")

    chunk_sets = [ln for ln in lines if ln.startswith("session.") and "Max-Age=0" not in ln]
    assert len(chunk_sets) >= 2, "expected multiple chunk cookies"
    # Every emitted chunk cookie line stays within the size limit.
    for ln in chunk_sets:
        assert len(ln) <= mw.max_cookie_size
    # The base cookie is deleted so the client keeps only the chunked encoding.
    assert any(ln.startswith("session=") and "Max-Age=0" in ln for ln in lines)


def test_chunked_session_round_trips_over_4kb():
    """End-to-end: stuff >4KB into the session, capture the chunk cookies, and
    send them back on a second request — the session reassembles intact."""
    app = Veloce()
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, chunked=True))

    big_value = "y" * 8192

    @app.post("/write")
    async def write(request: Request) -> Response:
        request.state["session"]["blob"] = big_value
        request.state["session"].modified = True
        return Response(200, b"ok")

    @app.get("/read")
    async def read(request: Request) -> Response:
        return Response(200, request.state["session"].get("blob", "MISSING").encode())

    with TestClient(app) as client:
        resp = client.post("/write")
        assert resp.status_code == 200
        # The client cookie jar now holds the chunk cookies (the base `session`
        # cookie was deleted via Max-Age=0 and dropped from the jar).
        jar = dict(client.cookies.items())
        assert "session" not in jar
        chunk_names = [n for n in jar if n.startswith("session.")]
        assert len(chunk_names) >= 2, "expected the session to span multiple chunks"

        # The jar auto-sends the chunks; the session reassembles intact.
        read_resp = client.get("/read")
        assert read_resp.body == big_value.encode()


async def test_shrinking_session_clears_stale_chunks():
    """A session that was chunked then shrinks to fit one cookie writes the
    single cookie AND deletes every chunk slot so no stale chunk lingers."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True)
    request = _req()
    session = Session({"user": "alice"})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    await mw.process_response(request, response)
    lines = response.headers["Set-Cookie"].split("\r\nSet-Cookie: ")

    # The single base cookie is set (non-deleting).
    assert any(ln.startswith("session=") and "Max-Age=0" not in ln for ln in lines)
    # Every chunk slot up to max_chunks is explicitly deleted.
    deleted_chunks = [ln for ln in lines if ln.startswith("session.") and "Max-Age=0" in ln]
    assert len(deleted_chunks) == mw.max_chunks


async def test_deleted_session_clears_base_and_all_chunks():
    """Emptying the session deletes the base cookie and every chunk slot."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True)
    request = _req()
    session = Session({"user": "alice"})
    session.clear()  # empties; marks modified
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    await mw.process_response(request, response)
    lines = response.headers["Set-Cookie"].split("\r\nSet-Cookie: ")

    assert any(ln.startswith("session=") and "Max-Age=0" in ln for ln in lines)
    deleted_chunks = [ln for ln in lines if ln.startswith("session.") and "Max-Age=0" in ln]
    assert len(deleted_chunks) == mw.max_chunks


async def test_chunk_request_reassembly_requires_contiguous_chunks():
    """A gap in the chunk sequence stops reassembly at the gap — a forged or
    truncated cookie set cannot reconstruct an arbitrary value."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True)
    # Build a real chunked cookie set, then drop chunk .1 to create a gap.
    request = _req()
    session = Session({"blob": "z" * 8192})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")
    await mw.process_response(request, response)

    cookies = {}
    for line in response.headers["Set-Cookie"].split("\r\nSet-Cookie: "):
        first = line.split(";", 1)[0]
        name, _, value = first.partition("=")
        if name.startswith("session.") and "Max-Age=0" not in line:
            cookies[name] = value
    assert "session.1" in cookies
    del cookies["session.1"]  # create a gap after .0

    header = "; ".join(f"{n}={v}" for n, v in cookies.items())
    read_req = _req(headers={"Cookie": header})
    await mw.process_request(read_req)
    # Reassembly stops at the gap, so the truncated token fails the signature
    # and the session is treated as new (empty).
    assert read_req.state["session"].new is True
    assert dict(read_req.state["session"]) == {}


async def test_exceeding_max_chunks_drops_with_warning(caplog):
    """A value that needs more than max_chunks cookies is dropped with a
    warning — no partial Set-Cookie."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True, max_chunks=2)
    request = _req()
    session = Session({"blob": "q" * 20000})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    with caplog.at_level(logging.WARNING, logger="veloce.sessions"):
        await mw.process_response(request, response)

    # No session.* cookie was set (the build aborted before any append).
    raw = response.headers.get("Set-Cookie")
    if raw:
        for line in raw.split("\r\nSet-Cookie: "):
            assert "Max-Age=0" in line or not line.startswith("session")
    matches = [r for r in caplog.records if r.name == "veloce.sessions"]
    assert matches and "max_chunks" in matches[-1].getMessage()


def test_max_chunks_constructor_validation():
    import pytest

    with pytest.raises(ValueError, match="max_chunks"):
        SessionMiddleware(secret_key="k" * 32, chunked=True, max_chunks=0)


def test_chunked_defaults():
    mw = SessionMiddleware(secret_key="k" * 32)
    assert mw.chunked is False
    assert mw.max_chunks == 8


async def test_exceeding_max_chunks_clears_stale_base_and_chunks():
    """When an oversized session gives up (needs > max_chunks cookies), the base
    cookie AND every chunk slot are deleted, so a previously-persisted smaller
    session is not silently resurrected from the client's stale cookies."""
    mw = SessionMiddleware(secret_key="k" * 32, chunked=True, max_chunks=2)
    request = _req()
    session = Session({"blob": "q" * 20000})
    session.modified = True
    request.state["session"] = session
    response = Response(200, b"ok")

    await mw.process_response(request, response)

    lines = response.headers["Set-Cookie"].split("\r\nSet-Cookie: ")
    # Base cookie cleared.
    assert any(ln.startswith("session=") and "Max-Age=0" in ln for ln in lines)
    # Every chunk slot cleared (no surviving payload from a prior persist).
    deleted_chunks = [ln for ln in lines if ln.startswith("session.") and "Max-Age=0" in ln]
    assert len(deleted_chunks) == mw.max_chunks
    # And no non-deletion session cookie leaked through.
    assert not any(ln.startswith("session") and "Max-Age=0" not in ln for ln in lines)
