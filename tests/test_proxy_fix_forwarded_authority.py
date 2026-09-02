"""A trusted `Forwarded` header is the sole authority over the legacy set.

`ProxyFix` resolved each directive as `Forwarded value OR X-Forwarded-* value`.
That reads as a sensible fallback and is exploitable: a `Forwarded` chain
shorter than the configured trust depth correctly yields nothing - failing
closed, as it should - and the fallback then handed that directive to whatever
the client wrote in `X-Forwarded-For`, which is exactly the value trust depth
had refused. An attacker could pick their own client IP simply by sending a
short `Forwarded` alongside a long `X-Forwarded-For`.

RFC 7239 Sec. 4 supersedes the `X-Forwarded-*` set for the directives it
defines - `for`, `proto` and `host` - so a trusted `Forwarded` is the sole
authority over those three. Port and prefix keep their fallback: the RFC spells
neither (a port rides inside `host="example.com:8443"`, and a path prefix has no
directive at all), so the legacy headers are the only channel a proxy has.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.middleware.proxy_fix import ProxyFix
from veloce.testclient import TestClient


def _client(**kwargs) -> TestClient:
    app = Veloce(openapi_url=None)
    # This module is about a *trusted* `Forwarded`, which is opt-in: the default
    # is `False` because nginx / ALB / most CDNs leave the header to the client.
    kwargs.setdefault("trust_forwarded", True)
    app.add_middleware(ProxyFix(**kwargs))

    @app.get("/who")
    async def who(request):  # noqa: ANN001, ANN202
        return {
            "client": request.client.host if request.client else None,
            "scheme": request.scheme,
            "host": request.host,
        }

    return TestClient(app)


# ── The reported bypass ──────────────────────────────────────────────


def test_a_short_forwarded_does_not_fall_back_to_the_legacy_chain():
    """The exploit: one `Forwarded` element, two attacker-written XFF hops."""
    with _client(x_for=2) as client:
        body = client.get(
            "/who",
            headers={
                "forwarded": "for=203.0.113.7",
                "x-forwarded-for": "203.0.113.7, 203.0.113.8",
            },
        ).json()
    assert body["client"] != "203.0.113.7", (
        "an attacker-chosen hop won after Forwarded failed closed"
    )


def test_a_short_forwarded_leaves_the_scheme_alone():
    with _client(x_for=2, x_proto=2) as client:
        body = client.get(
            "/who",
            headers={"forwarded": "for=203.0.113.7", "x-forwarded-proto": "https, https"},
        ).json()
    assert body["scheme"] == "http"


def test_a_short_forwarded_leaves_the_host_alone():
    with _client(x_for=2, x_host=2) as client:
        body = client.get(
            "/who",
            headers={"forwarded": "for=203.0.113.7", "x-forwarded-host": "a.example, evil.example"},
        ).json()
    assert body["host"] != "evil.example"


# ── A well-formed Forwarded still works ──────────────────────────────


def test_a_forwarded_chain_of_the_right_depth_is_honoured():
    with _client(x_for=2) as client:
        body = client.get(
            "/who",
            headers={"forwarded": "for=203.0.113.7, for=198.51.100.9"},
        ).json()
    assert body["client"] == "203.0.113.7"


def test_forwarded_carries_proto_and_host_together():
    with _client(x_for=1, x_proto=1, x_host=1) as client:
        body = client.get(
            "/who",
            headers={"forwarded": 'for=203.0.113.7; proto=https; host="app.example"'},
        ).json()
    assert body["client"] == "203.0.113.7"
    assert body["scheme"] == "https"
    assert body["host"] == "app.example"


# ── With no Forwarded, the legacy set is unchanged ───────────────────


def test_the_legacy_headers_still_work_on_their_own():
    with _client(x_for=1, x_proto=1) as client:
        body = client.get(
            "/who",
            headers={"x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https"},
        ).json()
    assert body["client"] == "203.0.113.7"
    assert body["scheme"] == "https"


def test_a_legacy_chain_still_selects_from_the_right():
    """Trust depth counts from the closest proxy; unchanged by this fix."""
    with _client(x_for=2) as client:
        body = client.get("/who", headers={"x-forwarded-for": "203.0.113.7, 198.51.100.9"}).json()
    assert body["client"] == "203.0.113.7"


def test_forwarded_is_ignored_entirely_when_not_trusted():
    """`trust_forwarded=False` means the legacy set governs, as before."""
    with _client(x_for=1, trust_forwarded=False) as client:
        body = client.get(
            "/who",
            headers={"forwarded": "for=198.51.100.9", "x-forwarded-for": "203.0.113.7"},
        ).json()
    assert body["client"] == "203.0.113.7"


def test_the_prefix_fallback_survives():
    """RFC 7239 has no `prefix` directive, so this channel must stay open."""
    with _client(x_for=1, x_prefix=1) as client:
        body = client.get(
            "/who",
            headers={"forwarded": "for=203.0.113.7", "x-forwarded-prefix": "/legacy"},
        ).json()
    assert body["client"] == "203.0.113.7"
