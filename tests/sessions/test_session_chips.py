"""Session middleware CHIPS (`Partitioned`) + `Domain` plumbing.

A partitioned session cookie (`Partitioned`, requiring `Secure` and
`SameSite=None`) is keyed to the embedding top-level site, so an embedded
third-party context gets an isolated session jar. Misconfiguration is
rejected at construction (fail-fast) rather than silently dropped.
"""

from __future__ import annotations

import pytest

from veloce import (
    InMemorySessionStore,
    Request,
    Response,
    ServerSessionMiddleware,
    SessionMiddleware,
    Veloce,
)


def _set_cookie_line(resp) -> str:
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie":
            return v
    return ""


def _cookie_app(**mw_kwargs) -> Veloce:
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, **mw_kwargs))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/clear")
    async def clear(request: Request):
        request.session.clear()
        return {"ok": True}

    return app


def _server_app(**mw_kwargs) -> Veloce:
    store = InMemorySessionStore()
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store, **mw_kwargs))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/clear")
    async def clear(request: Request):
        request.session.clear()
        return {"ok": True}

    return app


# ── Cookie-based SessionMiddleware ───────────────────────────────────


def test_cookie_session_partitioned_and_domain_on_write():
    client = _cookie_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    line = _set_cookie_line(client.get("/write"))
    assert "Domain=example.com" in line
    assert "Partitioned" in line


def test_cookie_session_partitioned_and_domain_on_clear():
    client = _cookie_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    client.get("/write")
    line = _set_cookie_line(client.get("/clear"))
    assert "Max-Age=0" in line
    assert "Partitioned" in line
    assert "Domain=example.com" in line


# ── ServerSessionMiddleware ──────────────────────────────────────────


def test_server_session_partitioned_and_domain_on_write():
    client = _server_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    line = _set_cookie_line(client.get("/write"))
    assert "Domain=example.com" in line
    assert "Partitioned" in line


def test_server_session_partitioned_and_domain_on_clear():
    client = _server_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    client.get("/write")
    line = _set_cookie_line(client.get("/clear"))
    assert "Max-Age=0" in line
    assert "Partitioned" in line
    assert "Domain=example.com" in line


# ── Construction-time fail-fast guards ───────────────────────────────


def test_cookie_session_partitioned_requires_secure():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, partitioned=True)


def test_cookie_session_partitioned_requires_samesite_none():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, secure=True, samesite="lax", partitioned=True)


def test_server_session_partitioned_requires_secure():
    with pytest.raises(ValueError):
        ServerSessionMiddleware(partitioned=True)


# ── `Partitioned` is emitted on the same terms as `Response.set_cookie` ───
#
# `SessionMiddleware._render_cookie` cannot call `set_cookie` (it needs the
# serialised line to size-check it before attaching), so it restates
# `set_cookie`'s `partitioned and secure` condition. These pin the two
# renderers to the same answer, including on the chunked path, which is the
# only place the hand-rolled renderer produces more than one line.


def _set_cookie_lines(resp) -> list[str]:
    return [v for k, v in resp.headers.items() if k.lower() == "set-cookie"][0].split(
        "\r\nSet-Cookie: "
    )


def test_cookie_session_partitioned_requires_samesite_none_not_omitted():
    """An omitted SameSite is not `None`; CHIPS needs the explicit value."""
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, secure=True, samesite=None, partitioned=True)


def test_cookie_session_partitioned_line_carries_secure_and_samesite_none():
    """The wire line must carry all three attributes together - `Partitioned`
    without `Secure` is rejected by browsers and costs the whole cookie."""
    client = _cookie_app(secure=True, samesite="none", partitioned=True).test_client()
    line = _set_cookie_line(client.get("/write"))
    assert "; Secure" in line
    assert "; SameSite=None" in line
    assert "; Partitioned" in line


def test_cookie_session_without_partitioned_omits_the_attribute():
    client = _cookie_app(secure=True, samesite="none").test_client()
    assert "Partitioned" not in _set_cookie_line(client.get("/write"))


@pytest.mark.parametrize("secure", [True, False])
@pytest.mark.parametrize("partitioned", [True, False])
def test_render_cookie_matches_set_cookie_for_every_partitioned_combination(secure, partitioned):
    """`_render_cookie` and `Response.set_cookie` agree on whether `Partitioned`
    is emitted, for every (secure, partitioned) pairing - including the invalid
    ones the constructor guard currently makes unreachable."""
    mw = SessionMiddleware(secret_key="k" * 32, secure=True, samesite="none", partitioned=True)
    # Assigned after construction on purpose: the guard rejects the invalid
    # pairings, and this pins what the renderer does if one is ever reached.
    mw.secure = secure
    mw.partitioned = partitioned
    rendered = mw._render_cookie("s", "v", 60, prefix=False)
    probe = Response()
    probe.set_cookie(
        "s",
        "v",
        max_age=60,
        path=mw.path,
        secure=secure,
        httponly=mw.httponly,
        samesite="None",
        partitioned=partitioned,
    )
    assert ("; Partitioned" in rendered) == ("; Partitioned" in probe.headers["Set-Cookie"])


def test_partitioned_without_secure_emits_no_partitioned_attribute():
    """Defence in depth behind the constructor guard: if `secure` is ever
    cleared after construction, the attribute must drop rather than ship a line
    every browser discards."""
    mw = SessionMiddleware(secret_key="k" * 32, secure=True, samesite="none", partitioned=True)
    mw.secure = False
    assert "; Partitioned" not in mw._render_cookie("s", "v", 60, prefix=False)


def test_every_session_chunk_carries_partitioned():
    """The chunked path renders one line per chunk through the same helper, so
    a partitioned session must not lose the attribute on the overflow cookies."""
    client = _cookie_app(
        secure=True, samesite="none", partitioned=True, chunked=True, max_cookie_size=256
    ).test_client()

    app = client.app

    @app.get("/big")
    async def big(request: Request):
        request.session["blob"] = "x" * 900
        return {"ok": True}

    lines = _set_cookie_lines(client.get("/big"))
    assert len(lines) > 1, lines
    assert all("; Partitioned" in line for line in lines), lines
    assert all("; Secure" in line for line in lines), lines
