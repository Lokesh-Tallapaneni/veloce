"""Findings from the `routing` / `security` / `serving` / `middleware` sweep.

Those four directories were the one scope no review agent covered. Three defects,
one per class the rest of this review found productive.

**1. A duplicate route `name=` silently stole the reverse entry.**
`on_duplicate` governs a collision on method+path. A collision on the *name* went
unguarded, so the second registration overwrote `_named_routes` with no signal:

    @app.get("/users", name="listing")
    @app.get("/posts", name="listing")

    url_for("listing")  ->  "/posts"      and /users is unreachable by name

A template rendering that link now points at the wrong page. Nothing raised,
warned, or logged. This repository's own guardrails already name the concern for
the blueprint merge — "prefix child route names to avoid clobbering parent
entries" — and that path is protected; plain registration was not.

Replacing a route at the *same* path must stay silent: the name legitimately
moves with the route it names. Only a *different* path taking an existing name is
a collision.

**2. `_compress_stream(self, stream, request)` never used `request`.** Private,
one call site, and the argument was threaded through for nothing.

**3. `CORSMiddleware`'s own `Usage::` example raised.** `allow_headers` defaults
to `["*"]`, which cannot be combined with `allow_credentials=True`, so the
example in the class docstring — which renders into the API reference — failed at
construction. The identical example was already fixed in `docs/guide/cors.md`;
the docstring copy was missed.

Recorded as a clean negative: the sweep found **no** false import-cycle comment in
this scope (the only two deferred imports are for the optional `watchfiles`
dependency), no write-only constructor parameter, and the JWT HMAC-only claim,
the `X-Forwarded-Proto` trust rules, the compressible-type set and the
no-buffering-of-streams claim all hold under measurement.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from veloce import CORSMiddleware, Veloce
from veloce.testclient import TestClient

# ── 1. a name collision is reported ──────────────────────────────────


def test_a_duplicate_name_on_a_different_path_warns(caplog):
    """The defect: the reverse entry was replaced in silence."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):

        @app.get("/posts", name="listing")
        async def posts() -> dict:
            return {}

    assert any("listing" in r.getMessage() for r in caplog.records)


def test_the_warning_names_both_paths(caplog):
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):

        @app.get("/posts", name="listing")
        async def posts() -> dict:
            return {}

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "/users" in message
    assert "/posts" in message


def test_replacing_a_route_at_the_same_path_stays_silent(caplog):
    """The name legitimately moves with the route it names."""
    app = Veloce(openapi_url=None, on_duplicate="override")

    @app.get("/users", name="listing")
    async def first() -> dict:
        return {}

    async def second() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):
        app.get("/users", name="listing")(second)

    assert not [r for r in caplog.records if "name" in (r.getMessage()).lower()]
    assert app.url_for("listing") == "/users"


def test_two_distinct_names_are_silent(caplog):
    app = Veloce(openapi_url=None)

    with caplog.at_level(logging.WARNING):

        @app.get("/users", name="users")
        async def users() -> dict:
            return {}

        @app.get("/posts", name="posts")
        async def posts() -> dict:
            return {}

    assert caplog.records == []


def test_the_last_registration_still_wins():
    """Reporting it must not change which route the name resolves to."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    @app.get("/posts", name="listing")
    async def posts() -> dict:
        return {}

    assert app.url_for("listing") == "/posts"


def test_both_routes_still_serve():
    """A name collision is a naming problem, not a routing one."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {"which": "users"}

    @app.get("/posts", name="listing")
    async def posts() -> dict:
        return {"which": "posts"}

    client = TestClient(app)
    assert client.get("/users").json() == {"which": "users"}
    assert client.get("/posts").json() == {"which": "posts"}


def test_a_blueprint_name_is_still_namespaced(caplog):
    """The merge path was already protected and must stay silent."""
    from veloce import Blueprint

    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    bp = Blueprint("shop", url_prefix="/shop")

    @bp.get("/posts", name="listing")
    async def posts() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):
        app.register_blueprint(bp)

    assert app.url_for("listing") == "/users"
    assert app.url_for("shop.listing") == "/shop/posts"


# ── 2. the dead parameter is gone ────────────────────────────────────


def test_compress_stream_takes_only_the_stream():
    from veloce.middleware.compression import GZipMiddleware

    params = list(inspect.signature(GZipMiddleware._compress_stream).parameters)
    assert params == ["self", "stream"]


def test_streaming_compression_still_works():
    """The negative: removing the argument must not disturb the path."""
    from veloce import GZipMiddleware, StreamingResponse

    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=1))

    @app.get("/s")
    async def s():
        async def gen():
            for _ in range(50):
                yield b"hello world "

        return StreamingResponse(gen(), content_type="text/plain")

    import gzip

    response = TestClient(app).get("/s", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("Content-Encoding") == "gzip"
    # The client does not decode for us, so the round trip is the assertion.
    assert gzip.decompress(response.body) == b"hello world " * 50


def test_a_refused_encoding_still_streams_plain():
    from veloce import GZipMiddleware, StreamingResponse

    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=1))

    @app.get("/s")
    async def s():
        async def gen():
            yield b"plain"

        return StreamingResponse(gen(), content_type="text/plain")

    response = TestClient(app).get("/s", headers={"Accept-Encoding": "gzip;q=0"})
    assert response.headers.get("Content-Encoding") != "gzip"
    assert response.text == "plain"


# ── 3. the CORS usage example runs ───────────────────────────────────


def _usage_block(obj) -> str:
    import textwrap

    doc = inspect.getdoc(obj) or ""
    after = doc.split("Usage::", 1)[1]
    lines, started = [], False
    for line in after.splitlines():
        if not line.strip():
            if started:
                lines.append("")
            continue
        if line.startswith((" ", "\t")):
            started = True
            lines.append(line)
        elif started:
            break
    return textwrap.dedent("\n".join(lines)).strip()


def test_the_cors_usage_example_constructs():
    """The defect: it raised `ValueError` at construction."""
    namespace = {"CORSMiddleware": CORSMiddleware, "app": Veloce(openapi_url=None)}
    exec(compile(_usage_block(CORSMiddleware), "<cors usage>", "exec"), namespace)


def test_the_cors_usage_example_declares_allow_headers():
    """Omitting it is the wildcard that made the example raise."""
    assert "allow_headers" in _usage_block(CORSMiddleware)


def test_credentials_with_wildcard_headers_is_still_refused():
    """The guard the example was tripping must stay."""
    with pytest.raises(ValueError, match="allow_credentials"):
        CORSMiddleware(allow_origins=["https://example.com"], allow_credentials=True)


def test_the_example_actually_answers_a_cors_request():
    """Constructing is not enough - it has to do the job it advertises."""
    app = Veloce(openapi_url=None)
    namespace = {"CORSMiddleware": CORSMiddleware, "app": app}
    exec(compile(_usage_block(CORSMiddleware), "<cors usage>", "exec"), namespace)

    @app.get("/x")
    async def x() -> dict:
        return {}

    response = TestClient(app).get("/x", headers={"Origin": "https://example.com"})
    allowed = [v for k, v in response.headers.items() if k.lower() == "access-control-allow-origin"]
    assert allowed == ["https://example.com"]


# ── the clean negatives, pinned ──────────────────────────────────────


def test_the_scope_defers_only_the_optional_dependency():
    """No deferred import in this scope claims to break a cycle."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/veloce"
    deferred = []
    for directory in ("routing", "security", "serving", "middleware"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [
                n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                for sub in ast.walk(fn):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        deferred.append(getattr(sub, "module", None) or sub.names[0].name)
    assert set(deferred) <= {"watchfiles"}, deferred


def test_alg_none_is_refused_even_when_allow_listed():
    """The JWT module's strongest claim, pinned."""
    import base64
    import json

    from veloce.security.jwt import decode_jwt

    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'attacker'})}."
    with pytest.raises(Exception, match="none"):
        decode_jwt(token, "secret", algorithms=["none"])
