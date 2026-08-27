"""`session_transaction()` seeds a session the middleware will actually read.

The failure this file exists for is the worst kind a test helper can have: it
made *other* tests pass while covering nothing. `session_transaction()` wrote the
cookie under `cookie_name`, and a middleware built with `cookie_prefix=` reads
only the prefixed `__Host-`/`__Secure-` name — so the seeded session was never
found, the request ran anonymous, and a test asserting authenticated behaviour
went green regardless.

Two more failures were reachable from the same helper: seeding before the first
request crashed when the signing key came from `app.config`, and a server-side
backend produced a message that sent the reader looking for a mistake they had
not made.
"""

from __future__ import annotations

import pytest

import veloce.signing
from veloce import ServerSessionMiddleware, SessionMiddleware, Veloce
from veloce.testclient import TestClient

KEY = "k" * 32


def _client(middleware, **config) -> TestClient:
    app = Veloce(openapi_url=None)
    app.config.update(config)
    app.add_middleware(middleware)

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    @app.get("/login")
    async def login(request):
        request.session["user"] = "from-handler"
        return {}

    return TestClient(app)


# ── the cookie prefix ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "expected_name"),
    [(None, "session"), ("secure", "__Secure-session"), ("host", "__Host-session")],
)
def test_the_seeded_cookie_uses_the_name_the_middleware_reads(prefix, expected_name):
    """The defect: seeded under the bare name, the prefixed cookie was never found."""
    client = _client(SessionMiddleware(secret_key=KEY, cookie_prefix=prefix, secure=True))
    with client.session_transaction() as session:
        session["user"] = "seeded"
    assert expected_name in client.cookies
    assert client.get("/who").json() == {"user": "seeded"}


def test_a_prefixed_session_survives_a_round_trip():
    """Reading an existing session back must use the same name as writing it."""
    client = _client(SessionMiddleware(secret_key=KEY, cookie_prefix="secure", secure=True))
    with client.session_transaction() as session:
        session["user"] = "first"
    with client.session_transaction() as session:
        assert session["user"] == "first"
        session["user"] = "second"
    assert client.get("/who").json() == {"user": "second"}


def test_an_unprefixed_session_still_works():
    client = _client(SessionMiddleware(secret_key=KEY))
    with client.session_transaction() as session:
        session["user"] = "plain"
    assert client.get("/who").json() == {"user": "plain"}


def test_a_custom_cookie_name_is_honoured():
    client = _client(SessionMiddleware(secret_key=KEY, cookie_name="sid"))
    with client.session_transaction() as session:
        session["user"] = "named"
    assert "sid" in client.cookies
    assert client.get("/who").json() == {"user": "named"}


# ── the signing key resolved from config ─────────────────────────────


def test_seeding_works_when_the_key_comes_from_app_config():
    """The defect: `_signer` is set on the first request, and this runs before one."""
    client = _client(SessionMiddleware(), SECRET_KEY=KEY)
    with client.session_transaction() as session:
        session["user"] = "from-config"
    assert client.get("/who").json() == {"user": "from-config"}


def test_seeding_works_when_the_key_is_on_the_constructor():
    client = _client(SessionMiddleware(secret_key=KEY))
    with client.session_transaction() as session:
        session["user"] = "explicit"
    assert client.get("/who").json() == {"user": "explicit"}


def test_seeding_after_a_request_still_works():
    """The key is already resolved by then; the path must not have regressed."""
    client = _client(SessionMiddleware(), SECRET_KEY=KEY)
    client.get("/who")
    with client.session_transaction() as session:
        session["user"] = "later"
    assert client.get("/who").json() == {"user": "later"}


def test_no_key_anywhere_says_what_to_do():
    """Reachable only by silencing the startup finding, which is a real choice."""
    app = Veloce(openapi_url=None)
    app.config["SILENCED_AUDIT_IDS"] = ("session-secret-key-missing",)
    app.add_middleware(SessionMiddleware())

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    client = TestClient(app)
    with pytest.raises(RuntimeError) as raised, client.session_transaction():
        pass
    # Both remedies, and when it was needed. The message comes from the
    # middleware's own key resolution now, so it cannot drift from the one a
    # first request would raise.
    message = str(raised.value)
    assert "secret_key=" in message
    assert "app.secret_key" in message
    assert "session_transaction()" in message


# ── the error messages ───────────────────────────────────────────────


def test_a_server_side_backend_says_why_it_cannot_be_seeded():
    """The generic message sent the reader hunting a mistake they had not made."""
    client = _client(ServerSessionMiddleware())
    with pytest.raises(RuntimeError, match="does not use"), client.session_transaction():
        pass


def test_the_server_side_message_names_the_middleware():
    client = _client(ServerSessionMiddleware())
    with pytest.raises(RuntimeError, match="ServerSessionMiddleware"), client.session_transaction():
        pass


def test_no_session_middleware_at_all_still_says_so():
    app = Veloce(openapi_url=None)
    client = TestClient(app)
    with (
        pytest.raises(RuntimeError, match="requires SessionMiddleware"),
        client.session_transaction(),
    ):
        pass


# ── the seeded session composes with the handler ─────────────────────


def test_a_handler_can_overwrite_a_seeded_session():
    client = _client(SessionMiddleware(secret_key=KEY, cookie_prefix="secure", secure=True))
    with client.session_transaction() as session:
        session["user"] = "seeded"
    client.get("/login")
    assert client.get("/who").json() == {"user": "from-handler"}


def test_a_session_written_by_a_handler_is_readable_by_the_helper():
    client = _client(SessionMiddleware(secret_key=KEY, cookie_prefix="secure", secure=True))
    client.get("/login")
    with client.session_transaction() as session:
        assert session["user"] == "from-handler"


def test_a_subclass_of_the_cookie_backend_is_accepted():
    class MySession(SessionMiddleware):
        pass

    client = _client(MySession(secret_key=KEY))
    with client.session_transaction() as session:
        session["user"] = "subclass"
    assert client.get("/who").json() == {"user": "subclass"}


def test_an_empty_session_seeds_cleanly():
    client = _client(SessionMiddleware(secret_key=KEY))
    with client.session_transaction():
        pass
    assert client.get("/who").json() == {"user": None}


# ── it goes through the middleware, not around it ────────────────────
#
# `session_transaction` used to carry its own copy of the middleware's key
# resolution and its own `Signer.loads` call. The copy differed: it passed
# `max_age=max(max_age, permanent_lifetime)` and stopped there, skipping the
# `_permanent`-dependent ceiling the request path applies. So a stale
# non-permanent cookie that a request would reject was still loaded here, and a
# test seeded from it saw a session the app would not have.


def test_a_seeded_cookie_is_one_a_request_accepts():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k" * 32
    app.add_middleware(SessionMiddleware())

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    client = TestClient(app)
    with client.session_transaction() as sess:
        sess["user"] = "alice"
    assert client.get("/who").json() == {"user": "alice"}


def test_a_prefixed_cookie_is_seeded_under_its_wire_name():
    """`__Host-` must be applied, or the seeded cookie is never found."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k" * 32
    app.add_middleware(SessionMiddleware(secure=True, cookie_prefix="host"))

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    client = TestClient(app)
    with client.session_transaction() as sess:
        sess["user"] = "alice"
    assert any(name.startswith("__Host-") for name in client._cookies)
    assert client.get("/who").json() == {"user": "alice"}


def test_a_stale_cookie_is_not_loaded_into_the_transaction():
    """The behaviour the duplicated decode got wrong: the seam applies the same
    age ceiling a request does, so a cookie the app would refuse starts empty
    here rather than arriving pre-populated."""

    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k" * 32
    middleware = SessionMiddleware(max_age=60, permanent_lifetime=60)
    app.add_middleware(middleware)

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    client = TestClient(app)
    middleware.bind_secret_key(app.config)
    real = veloce.signing.time.time
    veloce.signing.time.time = lambda: real() - 600
    try:
        stale = middleware.encode_cookie({"user": "alice"})
    finally:
        veloce.signing.time.time = real
    client._cookies[middleware.wire_cookie_name] = stale

    with client.session_transaction() as sess:
        assert dict(sess) == {}


def test_the_transaction_reads_back_what_it_wrote():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k" * 32
    app.add_middleware(SessionMiddleware())
    client = TestClient(app)
    with client.session_transaction() as sess:
        sess["n"] = 1
    with client.session_transaction() as sess:
        assert sess["n"] == 1
