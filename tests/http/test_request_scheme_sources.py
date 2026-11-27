"""Where `request.scheme` comes from, and what is allowed to set it.

Two defects lived in the same precedence chain.

The raw transport passes no ASGI scope, so nothing could tell a request it had
arrived over TLS: `app.run(ssl_context=...)` and the gunicorn worker both
terminate TLS on the connection itself, and `request.scheme` still answered
`"http"` on a genuinely encrypted connection - so `url_for(_external=True)`
emitted `http://` and every `scheme == "https"` check took the wrong branch.

Separately, `URL.from_request` read `X-Forwarded-Proto` directly whenever
nothing else supplied a scheme. That is a fair default with no `ProxyFix`
installed, but once `ProxyFix` HAS run it handed the scheme to a hop `ProxyFix`
had just refused - the trust depth bypassed by the very header it governs.

Order of authority, asserted below: the live connection, then what `ProxyFix`
trusted, then the bare header only when no `ProxyFix` ran.
"""

from __future__ import annotations

from tests.conftest import make_request
from veloce.http.datastructures import URL, Headers
from veloce.http.request import Request


class _Transport:
    """Stand in for an asyncio transport, with or without TLS."""

    def __init__(self, tls: bool) -> None:
        self._tls = tls

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "ssl_object":
            return object() if self._tls else None
        return default


def _request(*, tls: bool = False, headers: dict[str, str] | None = None) -> Request:
    return make_request(
        method="GET",
        path="/x",
        query_string="",
        headers=Headers(headers or {"host": "example.com"}),
        body=b"",
        transport=_Transport(tls),  # type: ignore[arg-type]
    )


# ── The live connection is authoritative ─────────────────────────────


def test_a_tls_connection_reports_https():
    """The defect: this answered "http" over real TLS."""
    assert _request(tls=True).scheme == "https"


def test_a_plaintext_connection_reports_http():
    assert _request(tls=False).scheme == "http"


def test_an_external_url_uses_the_connection_scheme():
    assert _request(tls=True).url.scheme == "https"
    assert "https://" in _request(tls=True).base_url


def test_the_connection_outranks_a_forwarded_header():
    """A header cannot be more authoritative than the socket it arrived on."""
    request = _request(tls=True, headers={"host": "example.com", "x-forwarded-proto": "http"})
    assert request.scheme == "https"


def test_a_request_with_no_transport_is_unchanged():
    """The ASGI path supplies a scope and never a transport."""
    request = make_request(
        method="GET",
        path="/x",
        query_string="",
        headers=Headers({"host": "example.com"}),
        body=b"",
        scope={"scheme": "https"},
    )
    assert request.scheme == "https"


def test_the_scope_still_wins_when_present():
    """ProxyFix writes the trusted scheme into the scope; that must hold."""
    request = make_request(
        method="GET",
        path="/x",
        query_string="",
        headers=Headers({"host": "example.com"}),
        body=b"",
        transport=_Transport(False),  # type: ignore[arg-type]
        scope={"scheme": "https"},
    )
    assert request.scheme == "https"


# ── The forwarded header is a default, not an override ───────────────


def test_the_header_is_honoured_when_no_proxyfix_ran():
    """Unchanged convenience behaviour for an app with no ProxyFix."""
    request = _request(headers={"host": "example.com", "x-forwarded-proto": "https"})
    assert request.scheme == "https"


def test_the_header_is_ignored_once_proxyfix_has_run():
    """The bypass: ProxyFix refused the hop, the raw header won anyway."""
    request = _request(headers={"host": "example.com", "x-forwarded-proto": "https"})
    request.state["proxy_fix_applied"] = True
    assert request.scheme == "http", "an untrusted hop set the scheme after ProxyFix refused it"


def test_a_trusted_proxyfix_scheme_still_applies():
    """Refusing the raw header must not break the trusted path."""
    request = _request(headers={"host": "example.com", "x-forwarded-proto": "https"})
    request.state["proxy_fix_applied"] = True
    request.scope["scheme"] = "https"
    request._url = None
    assert request.scheme == "https"


# ── The URL builder's own gate ───────────────────────────────────────


def test_from_request_trusts_the_header_by_default():
    url = URL.from_request(Headers({"host": "h", "x-forwarded-proto": "https"}), "/", "")
    assert url.scheme == "https"


def test_from_request_can_be_told_not_to_trust_the_header():
    url = URL.from_request(
        Headers({"host": "h", "x-forwarded-proto": "https"}),
        "/",
        "",
        trust_forwarded_proto=False,
    )
    assert url.scheme == "http"


def test_an_explicit_scheme_beats_the_gate_either_way():
    url = URL.from_request(
        Headers({"host": "h"}), "/", "", scope_scheme="https", trust_forwarded_proto=False
    )
    assert url.scheme == "https"
