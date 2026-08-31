"""StaticFiles HTTP Range support."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce.contrib.staticfiles import StaticFiles

_CONTENT = b"0123456789" * 10
_SIZE = len(_CONTENT)


@pytest.fixture()
def static(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(_CONTENT)
    return StaticFiles(directory=str(tmp_path), prefix="/static"), str(f)


def test_the_fixture_is_the_size_the_range_literals_assume():
    """The `Content-Range` values below spell the size out on the wire.

    They stay literal - `bytes 10-99/100` is what a client sees, and deriving
    it would make every assertion harder to read than the header it checks.
    Changing the fixture instead fails here, once, with a message that says so,
    rather than in six string comparisons that each look like a range bug.
    """
    assert _SIZE == 100


# ── Plain GET emits Accept-Ranges ────────────────────────────────────


async def test_plain_get_advertises_accept_ranges(static):
    sf, _ = static
    resp = await sf.handle(make_request(path="/static/blob.bin"))
    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert len(resp.body) == _SIZE


# ── Open-ended ranges ────────────────────────────────────────────────


async def test_range_open_end(static):
    """`bytes=10-` returns bytes 10..end."""
    sf, _ = static
    resp = await sf.handle(make_request(path="/static/blob.bin", headers={"range": "bytes=10-"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 10-99/100"
    assert resp.body == b"0123456789" * 9


async def test_range_closed(static):
    """`bytes=0-9` returns first 10 bytes inclusive."""
    sf, _ = static
    resp = await sf.handle(make_request(path="/static/blob.bin", headers={"range": "bytes=0-9"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 0-9/100"
    assert resp.body == b"0123456789"


async def test_range_suffix(static):
    """`bytes=-20` returns last 20 bytes."""
    sf, _ = static
    resp = await sf.handle(make_request(path="/static/blob.bin", headers={"range": "bytes=-20"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 80-99/100"
    assert resp.body == (b"0123456789" * 10)[-20:]


async def test_range_end_past_eof_clamped(static):
    """`bytes=90-1000` over a 100-byte file → 90-99/100, not 416."""
    sf, _ = static
    resp = await sf.handle(
        make_request(path="/static/blob.bin", headers={"range": "bytes=90-1000"})
    )
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 90-99/100"
    assert len(resp.body) == 10


# ── 416 for fully unsatisfiable ──────────────────────────────────────


async def test_range_start_past_eof_returns_416(static):
    sf, _ = static
    resp = await sf.handle(
        make_request(path="/static/blob.bin", headers={"range": "bytes=200-300"})
    )
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */100"


# ── Headers preserved alongside Content-Range ────────────────────────


async def test_partial_response_keeps_etag_and_last_modified(static):
    sf, _ = static
    resp = await sf.handle(make_request(path="/static/blob.bin", headers={"range": "bytes=0-9"}))
    assert resp.headers["ETag"].startswith('W/"')
    assert "Last-Modified" in resp.headers
    assert resp.headers["Accept-Ranges"] == "bytes"


# ── If-Range gate (RFC 9110 Sec. 13.1.5) ──────────────────────────────


async def test_if_range_weak_etag_serves_full_200(static):
    """RFC 9110 Sec. 13.1.5 mandates STRONG comparison for an If-Range ETag.
    Veloce emits weak file ETags, so even a byte-identical weak ETag cannot
    authorize a range resume - the server returns the full 200."""
    sf, _ = static
    etag = (await sf.handle(make_request(path="/static/blob.bin"))).headers["ETag"]
    assert etag.startswith("W/")  # the server's file ETags are weak
    resp = await sf.handle(
        make_request(path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": etag})
    )
    assert resp.status_code == 200
    assert len(resp.body) == _SIZE
    assert "Content-Range" not in resp.headers


async def test_if_range_exact_date_serves_206(static):
    """An If-Range HTTP-date that exactly matches the file's Last-Modified
    authorizes the range resume (RFC 9110 Sec. 13.1.5 exact-match rule)."""
    sf, _ = static
    last_modified = (await sf.handle(make_request(path="/static/blob.bin"))).headers[
        "Last-Modified"
    ]
    resp = await sf.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": last_modified}
        )
    )
    assert resp.status_code == 206
    assert len(resp.body) == 10


async def test_if_range_stale_etag_serves_full_200(static):
    """A Range with a stale If-Range ETag downgrades to a full 200, not a 206 slice."""
    sf, _ = static
    resp = await sf.handle(
        make_request(
            path="/static/blob.bin",
            headers={"Range": "bytes=0-9", "If-Range": '"stale-different-version"'},
        )
    )
    assert resp.status_code == 200
    assert len(resp.body) == _SIZE
    assert "Content-Range" not in resp.headers


async def test_if_range_stale_date_serves_full_200(static):
    """An If-Range HTTP-date older than the file's mtime downgrades to a full 200."""
    sf, _ = static
    resp = await sf.handle(
        make_request(
            path="/static/blob.bin",
            headers={"Range": "bytes=0-9", "If-Range": "Wed, 01 Jan 2020 00:00:00 GMT"},
        )
    )
    assert resp.status_code == 200
    assert len(resp.body) == _SIZE


# ── If-Range strong comparison (RFC 9110 Sec. 8.8.3.1) ────────────────
#
# The stock `_compute_etag` emits weak tags, so the strong-comparison branch is
# only reachable through a subclass that overrides it. These exercise that
# branch directly - it shares `_etag_matches_strong` with the `If-Match` gate,
# so the two validators cannot drift apart.


class _StrongEtagStatic(StaticFiles):
    """A StaticFiles whose ETags are strong, so If-Range can succeed."""

    def _compute_etag(self, path: str, size: int, mtime: float) -> str:
        return '"strong-v1"'


class _PaddedEtagStatic(StaticFiles):
    """A StaticFiles emitting a tag wrapped in OWS - RFC 9110 Sec. 5.5 excludes
    leading/trailing whitespace from a field value, so it must not defeat the
    comparison."""

    def _compute_etag(self, path: str, size: int, mtime: float) -> str:
        return ' "strong-v1" '


@pytest.fixture()
def strong_static(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789" * 10)
    return _StrongEtagStatic(directory=str(tmp_path), prefix="/static")


async def test_if_range_strong_etag_match_serves_206(strong_static):
    """Both validators strong and byte-identical - the resume is authorized."""
    resp = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": '"strong-v1"'}
        )
    )
    assert resp.status_code == 206
    assert resp.body == b"0123456789"


async def test_if_range_strong_etag_mismatch_serves_full_200(strong_static):
    """A different opaque tag means the representation changed - full 200."""
    resp = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": '"strong-v2"'}
        )
    )
    assert resp.status_code == 200
    assert len(resp.body) == _SIZE


async def test_if_range_weak_client_token_against_strong_server_serves_200(strong_static):
    """A `W/` marker on the client side alone defeats the strong comparison."""
    resp = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": 'W/"strong-v1"'}
        )
    )
    assert resp.status_code == 200
    assert len(resp.body) == _SIZE


async def test_if_range_lowercase_w_prefix_is_not_an_entity_tag(strong_static):
    """RFC 9110 Sec. 8.8.3 spells the weak marker `W/`, case-sensitively, so
    `w/"x"` parses as neither an entity-tag nor an HTTP-date. The unparseable
    If-Range is ignored and the Range stands - it must never be mistaken for a
    weak spelling of the server's own tag."""
    resp = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": 'w/"strong-v1"'}
        )
    )
    assert resp.status_code == 206
    assert resp.body == b"0123456789"


async def test_if_range_ows_padded_server_etag_still_matches(tmp_path):
    """OWS around a field value is not part of it (RFC 9110 Sec. 5.5), so a
    padded server tag still satisfies the client's echo of it."""
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789" * 10)
    sf = _PaddedEtagStatic(directory=str(tmp_path), prefix="/static")
    echoed = (await sf.handle(make_request(path="/static/blob.bin"))).headers["ETag"]
    resp = await sf.handle(
        make_request(path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": echoed})
    )
    assert resp.status_code == 206
    assert resp.body == b"0123456789"


async def test_if_range_star_is_ignored_and_range_is_honored(static):
    """`*` is neither an entity-tag nor an HTTP-date; RFC 9110 Sec. 13.1.5 says
    an unparseable If-Range is ignored, leaving the Range in force."""
    sf, _ = static
    resp = await sf.handle(
        make_request(path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": "*"})
    )
    assert resp.status_code == 206
    assert resp.body == b"0123456789"


async def test_if_range_agrees_with_if_match_on_the_same_validators(strong_static):
    """The two preconditions run one comparison function, so a token that
    satisfies If-Match also authorizes an If-Range resume."""
    ok = await strong_static.handle(
        make_request(path="/static/blob.bin", headers={"If-Match": '"strong-v1"'})
    )
    assert ok.status_code == 200
    resumed = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": '"strong-v1"'}
        )
    )
    assert resumed.status_code == 206

    denied = await strong_static.handle(
        make_request(path="/static/blob.bin", headers={"If-Match": 'W/"strong-v1"'})
    )
    assert denied.status_code == 412
    not_resumed = await strong_static.handle(
        make_request(
            path="/static/blob.bin", headers={"Range": "bytes=0-9", "If-Range": 'W/"strong-v1"'}
        )
    )
    assert not_resumed.status_code == 200
