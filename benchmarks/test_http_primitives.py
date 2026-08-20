"""HTTP primitive benchmarks — request construction and lazy parsing.

`Request` defers header, query-string, cookie, and JSON parsing until the
attribute is read, so each property is measured on a freshly built request
to keep the per-instance caches from hiding the work.
"""

from __future__ import annotations

from benchmarks.conftest import make_request
from veloce import Markup, escape, jsonable_encoder, secure_filename
from veloce.http.dates import http_date, parse_date

# A browser-shaped header set in the raw ASGI `(bytes, bytes)` form the
# server hands to `Request`, so the latin-1 decode is measured too.
RAW_HEADERS: list[tuple[bytes, bytes]] = [
    (b"host", b"api.example.com"),
    (b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0"),
    (b"accept", b"text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
    (b"accept-language", b"en-US,en;q=0.9,fr;q=0.8"),
    (b"accept-encoding", b"gzip, deflate, br"),
    (b"content-type", b"application/json"),
    (b"authorization", b"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"),
    (b"cookie", b"session=abc123; csrf_token=def456; theme=dark; locale=en_US"),
    (b"referer", b"https://example.com/previous/page"),
    (b"x-request-id", b"01HZ0000000000000000000000"),
]

QUERY_STRING = "q=veloce+framework&page=3&per_page=50&sort=-created_at&tags=a&tags=b&tags=c"

JSON_BODY = (
    b'{"name":"Thingamajig","price":19.99,"quantity":3,'
    b'"tags":["new","sale","featured"],"meta":{"sku":"TH-001","warehouse":"eu-west"}}'
)


# ── Construction ───────────────────────────────────────────


def test_request_construction(benchmark):
    """Build a `Request` — the allocation every inbound request pays."""
    request = benchmark(
        make_request,
        "POST",
        "/api/v1/items",
        RAW_HEADERS,
        JSON_BODY,
        QUERY_STRING,
    )
    assert request.method == "POST"


# ── Lazy parsing ───────────────────────────────────────────


def test_request_header_parsing(benchmark):
    """First `.headers` access: decode and index ten raw header tuples."""

    def parse() -> str:
        request = make_request(headers=RAW_HEADERS)
        return request.headers["user-agent"]

    assert benchmark(parse).startswith("Mozilla")


def test_request_query_params(benchmark):
    """First `.query_params` access: parse a multi-value query string."""

    def parse():
        request = make_request(query_string=QUERY_STRING)
        return request.query_params

    assert benchmark(parse)["page"] == "3"


def test_request_cookie_parsing(benchmark):
    """First `.cookies` access: split a four-cookie `Cookie` header."""

    def parse():
        request = make_request(headers=RAW_HEADERS)
        return request.cookies

    assert benchmark(parse)["session"] == "abc123"


def test_request_json_parsing(benchmark):
    """First `.get_json()` access: orjson-decode a nested body."""

    def parse():
        request = make_request(
            method="POST",
            headers={"content-type": "application/json"},
            body=JSON_BODY,
        )
        return request.get_json()

    assert benchmark(parse)["price"] == 19.99


def test_request_accept_negotiation(benchmark):
    """Parse `Accept` and pick the best match — content negotiation."""

    def negotiate() -> str | None:
        request = make_request(headers=RAW_HEADERS)
        return request.accept_mimetypes.best_match(["application/json", "text/html"])

    assert benchmark(negotiate) is not None


def test_request_url_build(benchmark):
    """Reconstruct the absolute URL from the scope pieces."""

    def build() -> str:
        request = make_request(path="/api/v1/items", headers=RAW_HEADERS, query_string=QUERY_STRING)
        return str(request.url)

    assert "api.example.com" in benchmark(build)


# ── Small utilities on the hot path ────────────────────────


def test_http_date_format(benchmark):
    """Format an RFC 7231 date — every response with `Date`/`Expires`."""
    assert benchmark(http_date, 1_700_000_000).endswith("GMT")


def test_http_date_parse(benchmark):
    """Parse an RFC 7231 date — conditional-request handling."""
    assert benchmark(parse_date, "Tue, 14 Nov 2023 22:13:20 GMT") is not None


def test_markup_escape(benchmark):
    """HTML-escape a mixed string — template rendering and error pages."""
    raw = "A <script>alert(\"xss\")</script> & some 'quoted' text, repeated. " * 4
    assert isinstance(benchmark(escape, raw), Markup)


def test_secure_filename(benchmark):
    """Sanitize an upload filename."""
    assert benchmark(secure_filename, "../../etc/pass wd/Ünïcødé Report (final).tar.gz")


def test_jsonable_encoder_primitives(benchmark):
    """Encoder floor: a payload that needs no coercion at all."""
    payload = {"id": 1, "name": "x", "ok": True, "score": 1.5, "none": None}
    assert benchmark(jsonable_encoder, payload) == payload
