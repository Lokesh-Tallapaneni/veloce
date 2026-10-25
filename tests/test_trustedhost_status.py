"""`TrustedHostMiddleware(status_code=...)` — what a refused Host reports.

The refusal was hard-coded at `400 Bad Request`, which is the right default: the
request named a host this server does not answer for, so the request is
malformed rather than the credential it carried. Some deployments want `403`
instead, because an edge proxy, a WAF or a log pipeline keys on it. Only the
default is settled, not the status.

The guard is that the status stays a 4xx: a 2xx would report the rejection as a
success, a 3xx would send the client somewhere, and a 5xx would invite a retry
loop over a request that can never succeed.
"""

from __future__ import annotations

import pytest

from veloce import TrustedHostMiddleware, Veloce
from veloce.testclient import TestClient


def _app(allowed: list[str], **kwargs: object) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(TrustedHostMiddleware(allowed_hosts=allowed, **kwargs))  # type: ignore[arg-type]

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


# ── the status is configurable ───────────────────────────────────────


def test_a_configured_status_is_what_a_bad_host_gets():
    client = TestClient(_app(["example.com"], status_code=403))
    assert client.get("/x", headers={"host": "evil.example"}).status_code == 403


def test_any_other_4xx_is_accepted():
    client = TestClient(_app(["example.com"], status_code=404))
    assert client.get("/x", headers={"host": "evil.example"}).status_code == 404


def test_a_configured_status_does_not_reject_an_allowed_host():
    client = TestClient(_app(["example.com"], status_code=403))
    assert client.get("/x", headers={"host": "example.com"}).status_code == 200


# ── the default is unchanged ─────────────────────────────────────────


def test_the_default_is_still_400_with_the_same_body():
    client = TestClient(_app(["example.com"]))
    response = client.get("/x", headers={"host": "evil.example"})
    assert response.status_code == 400
    assert response.body == b"Invalid host header"


def test_a_configured_status_keeps_the_same_body():
    client = TestClient(_app(["example.com"], status_code=403))
    assert client.get("/x", headers={"host": "evil.example"}).body == b"Invalid host header"


def test_an_allowed_host_still_passes_through():
    client = TestClient(_app(["example.com"]))
    assert client.get("/x", headers={"host": "example.com"}).status_code == 200


def test_a_wildcard_host_still_passes_through():
    client = TestClient(_app(["*.example.com"], status_code=403))
    assert client.get("/x", headers={"host": "api.example.com"}).status_code == 200


# ── a non-4xx status is refused at construction ──────────────────────


@pytest.mark.parametrize("status_code", [200, 204, 301, 302, 500, 503, 0, 399, 600])
def test_a_status_outside_4xx_is_refused(status_code: int):
    with pytest.raises(ValueError):
        TrustedHostMiddleware(allowed_hosts=["example.com"], status_code=status_code)


def test_the_boundary_statuses_are_accepted():
    TrustedHostMiddleware(allowed_hosts=["example.com"], status_code=400)
    TrustedHostMiddleware(allowed_hosts=["example.com"], status_code=499)
