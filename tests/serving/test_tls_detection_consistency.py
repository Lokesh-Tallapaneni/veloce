"""One answer to "is this connection encrypted".

The question was answered in five places: `URL.from_request` compared
`X-Forwarded-Proto` case-sensitively, `Request.url` probed the raw transport's
TLS object, `Request.is_secure` did not know `wss`, `HTTPSRedirectMiddleware`
re-derived the whole thing case-insensitively and without the transport probe,
and `ProxyFix` wrote a trusted hop's scheme into the scope unnormalised.

The copies disagreed in the worst possible direction: a request that arrived
over TLS was 308-redirected to the URL it had just been served on. Serving TLS
natively did it, and so did any proxy that spelled the scheme in a casing other
than lowercase.

The scheme is now normalised where it is resolved, `SECURE_URL_SCHEMES` is the
single membership test, and the redirect guard reads `request.is_secure`
instead of restating it. The one thing the guard still adds is deliberate and
pinned below: it accepts an untrusted `X-Forwarded-Proto` that `is_secure` does
not, because a TLS-terminating proxy which does not set the ASGI scope would
otherwise have every request bounced back to a URL it already served over TLS.
"""

from __future__ import annotations

import pytest

from veloce.http.request import Request
from veloce.middleware.proxy_fix import ProxyFix
from veloce.middleware.security import HTTPSRedirectMiddleware


class _TLSTransport:
    """A raw transport that has completed a TLS handshake."""

    def get_extra_info(self, name, default=None):
        return object() if name == "ssl_object" else default


def _request(**kw) -> Request:
    kw.setdefault("headers", {"Host": "app.example.com"})
    return Request("GET", kw.pop("path", "/dash"), "", kw.pop("headers"), b"", **kw)


async def _redirect(request: Request):
    return await HTTPSRedirectMiddleware().process_request(request)


# ── The loop: a request that arrived over TLS is redirected to itself ──


async def test_a_natively_served_tls_request_is_not_redirected():
    """The defect: `app.run(ssl_context=...)` 308-looped every request."""
    request = _request(transport=_TLSTransport(), scope=None)
    assert request.is_secure is True
    assert await _redirect(request) is None


async def test_an_uppercase_forwarded_scheme_is_not_redirected():
    """The defect: a proxy spelling `HTTPS` looped every request."""
    request = _request(
        headers={
            "Host": "app.example.com",
            "X-Forwarded-Proto": "HTTPS",
            "X-Forwarded-For": "1.2.3.4",
        },
        scope={"scheme": "http"},
    )
    await ProxyFix(x_for=1, x_proto=1, x_host=1).process_request(request)
    assert request.scheme == "https"
    assert request.is_secure is True
    assert await _redirect(request) is None


@pytest.mark.parametrize("scheme", ["https", "HTTPS", "Https", "wss", "WSS"])
async def test_every_spelling_of_an_encrypted_scheme_agrees(scheme):
    request = _request(scope={"scheme": scheme})
    assert request.is_secure is True
    assert request.scheme == scheme.lower()
    assert await _redirect(request) is None


# ── What must still redirect ─────────────────────────────────────────


@pytest.mark.parametrize("scope", [{"scheme": "http"}, {"scheme": "HTTP"}, {}, None])
async def test_a_cleartext_request_is_still_redirected(scope):
    response = await _redirect(_request(scope=scope))
    assert response is not None
    assert response.status_code == 308
    assert response.headers["Location"] == "https://app.example.com/dash"


# ── The guard's deliberate extra, and its limit ──────────────────────


@pytest.mark.parametrize("value", ["https", "HTTPS", "Https"])
async def test_an_untrusted_forwarded_scheme_still_suppresses_the_redirect(value):
    """A TLS proxy that does not set the ASGI scope must not be looped.

    `is_secure` reads `http` here - the header is a claim about the hop in
    front, not about this connection - so the guard's own fallback is what
    keeps this deployment working. Removing it would loop every request.
    """
    request = _request(
        headers={"Host": "app.example.com", "X-Forwarded-Proto": value},
        scope={"scheme": "http"},
    )
    assert request.is_secure is False
    assert await _redirect(request) is None


async def test_a_stripped_scheme_cannot_suppress_the_redirect_once_proxyfix_ran():
    """The security half: an attacker-added header must not disarm the guard."""
    request = _request(
        headers={"Host": "app.example.com", "X-Forwarded-Proto": "https"},
        scope={"scheme": "http"},
    )
    # ProxyFix trusts no hops here, so it refuses the header and says so.
    await ProxyFix(x_for=0, x_proto=0, x_host=0).process_request(request)
    assert request.is_secure is False
    response = await _redirect(request)
    assert response is not None
    assert response.status_code == 308


# ── The scheme is normalised for every consumer, not just the guard ──


async def test_an_external_url_is_built_with_a_normalised_scheme():
    """RFC 3986 Sec. 3.1: lowercase is the normalised form."""
    request = _request(scope={"scheme": "HTTPS"})
    assert str(request.url).startswith("https://")


async def test_proxyfix_writes_a_normalised_scheme_into_the_scope():
    """Anything reading the raw scope sees what `request.scheme` sees."""
    scope = {"scheme": "http"}
    request = _request(
        headers={
            "Host": "app.example.com",
            "X-Forwarded-Proto": "HTTPS",
            "X-Forwarded-For": "1.2.3.4",
        },
        scope=scope,
    )
    await ProxyFix(x_for=1, x_proto=1, x_host=1).process_request(request)
    assert scope["scheme"] == "https"
