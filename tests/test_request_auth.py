"""Parsed `Request.auth` / Authorization tests."""

from __future__ import annotations

import base64

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.testclient import TestClient


def _req(authz: str | None = None) -> Request:
    headers = {"authorization": authz} if authz else {}
    return make_request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── Basic ─────────────────────────────────────────────────────────────


def test_basic_decoded():
    creds = base64.b64encode(b"alice:s3cret").decode()
    auth = _req(f"Basic {creds}").auth
    assert auth is not None
    assert auth.type == "basic"
    assert auth.username == "alice"
    assert auth.password == "s3cret"


def test_basic_with_empty_password():
    creds = base64.b64encode(b"alice:").decode()
    auth = _req(f"Basic {creds}").auth
    assert auth.username == "alice"
    assert auth.password == ""


def test_basic_without_colon_yields_no_credentials():
    """RFC 7617 Sec. 2 makes the colon mandatory, so this is a malformed header.

    This previously asserted `username == "justname"`, pinning a defect: the
    scheme that consumes the same header, `HTTPBasic`, answered it with a 401,
    so code reading `request.auth` saw a username for credentials the
    application had refused. A malformed payload now parses like undecodable
    base64 already did in the next test - scheme reported, credentials not.
    """
    creds = base64.b64encode(b"justname").decode()
    auth = _req(f"Basic {creds}").auth
    assert auth.type == "basic"
    assert auth.username is None
    assert auth.password is None


def test_basic_invalid_base64_returns_type_only():
    auth = _req("Basic !!!notbase64").auth
    assert auth.type == "basic"
    assert auth.username is None


# ── Bearer ────────────────────────────────────────────────────────────


def test_bearer_token_reaches_the_auth_property():
    auth = _req("Bearer abc.def.ghi").auth
    assert auth.type == "bearer"
    assert auth.token == "abc.def.ghi"
    assert auth.username is None
    assert auth.password is None


def test_bearer_strips_whitespace():
    auth = _req("Bearer    spaced    ").auth
    assert auth.token == "spaced"


# ── Digest / custom schemes ───────────────────────────────────────────


def test_digest_params_parsed():
    header = 'Digest username="alice", realm="api", nonce="xyz", response="abc"'
    auth = _req(header).auth
    assert auth.type == "digest"
    assert auth.params["username"] == "alice"
    assert auth.params["realm"] == "api"
    assert auth.params["nonce"] == "xyz"
    assert auth.params["response"] == "abc"


def test_digest_quoted_comma_in_value():
    """Values containing commas inside quotes must not split into new pairs."""
    header = 'Digest realm="a,b", username="c"'
    auth = _req(header).auth
    assert auth.params["realm"] == "a,b"
    assert auth.params["username"] == "c"


def test_custom_single_token_scheme():
    """A scheme with no `=` in credentials carries the credentials in `.token`."""
    auth = _req("Custom opaque-blob").auth
    assert auth.type == "custom"
    assert auth.token == "opaque-blob"


def test_unknown_singleton_header():
    """No space → single-token form."""
    auth = _req("AbcDef").auth
    assert auth.type == "abcdef"
    assert auth.token is None


# ── Missing header ────────────────────────────────────────────────────


def test_no_header_returns_none():
    assert _req().auth is None


def test_an_empty_header_leaves_the_auth_property_none():
    assert _req("").auth is None


# ── Raw .authorization preserved ──────────────────────────────────────


def test_raw_authorization_unchanged():
    """The raw string `request.authorization` is preserved for back-compat."""
    req = _req("Bearer xyz")
    assert req.authorization == "Bearer xyz"
    # The parsed view is separate.
    assert req.auth.token == "xyz"


def test_scheme_preserves_original_case():
    auth = _req("Bearer abc").auth
    assert auth.scheme == "Bearer"
    assert auth.type == "bearer"


# ── Cached identity ───────────────────────────────────────────────────


def test_auth_cached_identity():
    """Repeated reads of `.auth` return the same parsed object."""
    req = _req("Bearer abc.def.ghi")
    assert req.auth is req.auth
    # Missing-header path: cached `None` is still cached (no re-parse).
    miss = _req()
    assert miss.auth is None
    assert miss.auth is miss.auth


def test_auth_property_cached_across_repeat_access():
    """The end-to-end counterpart of `test_auth_cached_identity`.

    Caching also holds on a `Request` the ASGI path built, not only on
    one this module builds by hand.
    """
    app = Veloce(openapi_url=None)
    observed = {}

    @app.get("/auth")
    async def auth_route(request: Request):
        a1 = request.auth
        a2 = request.auth
        observed["same"] = a1 is a2
        observed["scheme"] = a1.type if a1 else None
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/auth", headers={"Authorization": "Bearer abc"})
    assert observed["same"] is True
    assert observed["scheme"] == "bearer"
