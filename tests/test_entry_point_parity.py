"""Two ways in, one answer: the same input gets the same treatment either way.

Three cases where one entry point validated or parsed something and its sibling,
reading the same input for the same purpose, did not.

1. `from_prefixed_env` read the same settings from the same environment as
   `from_env_file` and skipped the type coercion that loader exists to apply.
   `VELOCE_MAX_CONTENT_LENGTH=10MB` is not valid JSON, so it was stored as a
   string; the app booted clean and every request carrying a body then died on
   `'>' not supported between instances of 'int' and 'str'`.

2. `Blueprint.errorhandler` accepted any key. `_exception_handlers` is matched by
   an MRO walk, so a string key can never be found - `@bp.errorhandler("500")`
   sat in the table and never fired. Every app-level entry point refuses it.

3. `GZipMiddleware` honoured `q=0` but not `Q=0`. RFC 9110 Sec. 12.4.2's `weight`
   rule is an ABNF string literal and RFC 5234 Sec. 2.3 makes those
   case-insensitive, so `Q=0` is a refusal. The framework's own `AcceptHeader`
   next door reads both spellings; the middleware's hand-rolled copy read one,
   and a client that explicitly refused gzip got a gzipped body.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, GZipMiddleware, Veloce
from veloce.config import Config
from veloce.testclient import TestClient


def _config() -> Config:
    config = Config()
    config.update(Config.default_config())
    return config


# ── 1. both env loaders coerce to the declared type ──────────────────


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("MAX_CONTENT_LENGTH", "1000", 1000),
        ("DEBUG", "False", False),
        ("DEBUG", "true", True),
        ("MCP_CALL_TIMEOUT", "30", 30),
    ],
)
def test_from_prefixed_env_gives_a_value_its_declared_type(monkeypatch, key, raw, expected):
    monkeypatch.setenv(f"VELOCE_{key}", raw)
    config = _config()
    config.from_prefixed_env()
    assert config[key] == expected
    assert type(config[key]) is type(expected)


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("MAX_CONTENT_LENGTH", "lots"),
        ("MAX_CONTENT_LENGTH", "10MB"),
        ("MCP_CALL_TIMEOUT", "5s"),
        ("DEBUG", "banana"),
    ],
)
def test_from_prefixed_env_refuses_a_value_of_the_wrong_type(monkeypatch, key, raw):
    """The defect: these were stored as strings and failed per request instead."""
    monkeypatch.setenv(f"VELOCE_{key}", raw)
    config = _config()
    with pytest.raises(ValueError, match=key):
        config.from_prefixed_env()


@pytest.mark.parametrize(
    ("key", "raw"),
    [("MAX_CONTENT_LENGTH", "lots"), ("MCP_CALL_TIMEOUT", "5s"), ("DEBUG", "banana")],
)
def test_the_two_env_loaders_agree(tmp_path, monkeypatch, key, raw):
    """The property: same setting, same value, same outcome."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}={raw}\n", encoding="utf-8")
    from_file = pytest.raises(ValueError)
    with from_file:
        _config().from_env_file(str(env_file))

    monkeypatch.setenv(f"VELOCE_{key}", raw)
    with pytest.raises(ValueError):
        _config().from_prefixed_env()


def test_a_json_value_is_still_decoded(monkeypatch):
    """The existing behaviour: valid JSON wins and is not re-parsed as a string."""
    monkeypatch.setenv("VELOCE_SOME_LIST", "[1, 2, 3]")
    config = _config()
    config.from_prefixed_env()
    assert config["SOME_LIST"] == [1, 2, 3]


def test_an_unknown_key_is_still_stored_as_read(monkeypatch):
    """No declared type means nothing to coerce to."""
    monkeypatch.setenv("VELOCE_CUSTOM_THING", "hello world")
    config = _config()
    config.from_prefixed_env()
    assert config["CUSTOM_THING"] == "hello world"


def test_a_nested_key_is_unchanged(monkeypatch):
    monkeypatch.setenv("VELOCE_MAIL__SERVER", "smtp.example.com")
    config = _config()
    config.from_prefixed_env()
    assert config["MAIL"] == {"SERVER": "smtp.example.com"}


def test_a_body_request_works_after_loading_the_limit(monkeypatch):
    """End to end: the failure this prevents was a 500 on every body request."""
    monkeypatch.setenv("VELOCE_MAX_CONTENT_LENGTH", "1000000")
    app = Veloce(openapi_url=None)
    app.config.from_prefixed_env()

    @app.post("/x")
    async def x(request) -> dict:
        return {"n": len(await request.body())}

    assert TestClient(app).post("/x", json={"a": 1}).json()["n"] > 0


# ── 2. a blueprint refuses an unmatchable error-handler key ──────────


@pytest.mark.parametrize("key", ["500", "ValueError", 404.0, object, None, ("a",)])
def test_a_blueprint_refuses_an_unmatchable_key(key):
    """The defect: these were accepted and then never fired."""
    bp = Blueprint("shop")
    with pytest.raises(TypeError, match="error handler keys"):
        bp.errorhandler(key)(lambda exc: None)


@pytest.mark.parametrize("key", [500, 404, ValueError, KeyError, Exception])
def test_a_blueprint_accepts_a_valid_key(key):
    bp = Blueprint("shop")
    bp.errorhandler(key)(lambda exc: None)


def test_the_string_message_says_what_to_write_instead():
    bp = Blueprint("shop")
    with pytest.raises(TypeError, match="Write 500 without the quotes"):
        bp.errorhandler("500")(lambda exc: None)

    with pytest.raises(TypeError, match="Pass the class itself"):
        bp.errorhandler("ValueError")(lambda exc: None)


def test_the_two_registration_levels_agree():
    """The property: what the app refuses, a blueprint must refuse."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop")
    for key in ("500", "ValueError", 404.0):
        with pytest.raises(TypeError):
            app.errorhandler(key)(lambda exc: None)
        with pytest.raises(TypeError):
            bp.errorhandler(key)(lambda exc: None)


def test_a_valid_blueprint_handler_still_fires():
    """The negative: the check must not break registration that worked."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop", url_prefix="/shop")

    @bp.errorhandler(ValueError)
    async def on_value(exc):
        return {"handled": "by-class-key"}

    @bp.get("/boom")
    async def boom() -> dict:
        raise ValueError("nope")

    app.register_blueprint(bp)
    assert TestClient(app).get("/shop/boom").json() == {"handled": "by-class-key"}


# ── 3. gzip reads both spellings of the weight parameter ─────────────


def _gzip_app() -> TestClient:
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=100))

    @app.get("/x")
    async def x():
        from veloce import Response

        return Response(body=b"x" * 5000, content_type="text/plain")

    return TestClient(app)


@pytest.mark.parametrize(
    "accept",
    ["gzip;q=0", "gzip;Q=0", "gzip; Q=0", "gzip;Q=0.0", "*;q=0", "*;Q=0", "identity;q=1, gzip;Q=0"],
)
def test_a_refusal_is_honoured_in_either_spelling(accept):
    """The defect: the upper-case spellings got a gzipped body anyway."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") != "gzip"


@pytest.mark.parametrize("accept", ["gzip", "gzip;q=1", "gzip;Q=1", "gzip;q=0.5", "*"])
def test_an_acceptance_still_compresses(accept):
    """The negative: refusing everything would pass the test above vacuously."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") == "gzip"


@pytest.mark.parametrize("accept", ["gzip;q=bad", "gzip;Q=bad"])
def test_an_unparseable_weight_is_ignored_in_either_spelling(accept):
    """A malformed weight is not a refusal; both spellings must agree on that."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") == "gzip"


@pytest.mark.parametrize("spelling", ["q", "Q"])
def test_the_middleware_agrees_with_the_shared_parser(spelling):
    """`request.accept_encodings` already read both; the middleware is the copy that did not."""
    header = f"gzip;{spelling}=0"
    seen: dict = {}

    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(request) -> dict:
        seen["quality"] = request.accept_encodings.quality("gzip")
        return {}

    TestClient(app).get("/q", headers={"Accept-Encoding": header})
    assert seen["quality"] == 0.0
    response = _gzip_app().get("/x", headers={"Accept-Encoding": header})
    assert response.headers.get("Content-Encoding") != "gzip"
