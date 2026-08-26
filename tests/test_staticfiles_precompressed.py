"""StaticFiles precompressed sibling serving (br/gz)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request
from veloce.contrib.staticfiles import StaticFiles


def _req(path: str, headers: dict | None = None) -> Request:
    return make_request(
        method="GET",
        path=path,
        query_string="",
        headers=headers or {},
        body=b"",
    )


@pytest.mark.asyncio
async def test_serves_br_when_accepted(tmp_path):
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    (tmp_path / "app.css.br").write_bytes(b"BR-COMPRESSED")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br, gzip"}))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "br"
    assert resp.headers["Vary"] == "Accept-Encoding"
    assert resp.body == b"BR-COMPRESSED"
    # Media type stays that of the original asset, not the compressed wrapper.
    assert resp.content_type.startswith("text/css")


@pytest.mark.asyncio
async def test_qvalue_picks_gzip_over_br(tmp_path):
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    (tmp_path / "app.css.gz").write_bytes(b"GZIP")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=0.1, gzip;q=0.9"}))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.body == b"GZIP"


@pytest.mark.asyncio
async def test_falls_back_to_next_accepted_variant(tmp_path):
    # The highest-q encoding (br) has no sibling on disk, but a lower-q accepted
    # one (gzip) does. The server must fall through to gzip rather than serve
    # the uncompressed asset (RFC 9110 Sec. 12.5.3).
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    (tmp_path / "app.css.gz").write_bytes(b"GZIP")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=1, gzip;q=0.5"}))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.headers["Vary"] == "Accept-Encoding"
    assert resp.body == b"GZIP"


@pytest.mark.asyncio
async def test_qzero_rejects_encoding(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=0"}))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_explicit_qzero_overrides_wildcard(tmp_path):
    # RFC 9110 Sec. 12.5.3: an explicit `br;q=0` is a rejection that must
    # override the `*;q=1` wildcard. The buggy MAX-across-exact-and-wildcard
    # `quality()` would score br at 1.0 and serve the `.br` sibling; the fix
    # honors the explicit q=0 and falls back to gzip (acceptable via `*`).
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    (tmp_path / "app.css.gz").write_bytes(b"GZIP")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=0, *;q=1"}))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.body == b"GZIP"


@pytest.mark.asyncio
async def test_explicit_qzero_all_codings_serves_identity(tmp_path):
    # Both codings explicitly rejected via q=0 even though `*;q=1` is present:
    # no acceptable variant remains, so the raw asset is served.
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    (tmp_path / "app.css.gz").write_bytes(b"GZIP")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=0, gzip;q=0, *;q=1"}))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_wildcard_only_selects_variant(tmp_path):
    # A plain `*` with no explicit coding still selects a variant (br preferred
    # over gzip on the q tie via PRECOMPRESSED_VARIANTS order).
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    (tmp_path / "app.css.gz").write_bytes(b"GZIP")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "*"}))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "br"
    assert resp.body == b"BR"


@pytest.mark.asyncio
async def test_no_sibling_falls_through(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br, gzip"}))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_disabled_by_default(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br, gzip"}))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_etag_matches_compressed_bytes(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR-COMPRESSED")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    first = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br"}))
    etag = first.headers["ETag"]
    second = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br", "If-None-Match": etag}))
    assert second.status_code == 304


@pytest.mark.asyncio
async def test_range_over_precompressed(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BRCOMPRESSEDBYTES")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br", "Range": "bytes=0-3"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Encoding"] == "br"
    assert resp.headers["Vary"] == "Accept-Encoding"
    # Range is over the compressed length (len("BRCOMPRESSEDBYTES") == 17).
    assert resp.headers["Content-Range"] == "bytes 0-3/17"
    assert resp.body == b"BRCO"


@pytest.mark.asyncio
async def test_missing_accept_encoding_serves_raw(tmp_path):
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    # No Accept-Encoding header at all: the quality>0 gate must reject br.
    resp = await sf.handle(_req("/s/app.css"))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_identity_precompressed_carries_vary(tmp_path):
    # An asset with precompressed enabled is content-negotiated on
    # Accept-Encoding even when the identity body is served (no acceptable
    # encoding). The identity response must still carry `Vary: Accept-Encoding`
    # so a shared cache does not replay it to a compression-capable client
    # (RFC 9110 Sec. 12.5.5).
    (tmp_path / "app.css").write_bytes(b"RAW")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css"))
    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert resp.headers["Vary"] == "Accept-Encoding"
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_identity_precompressed_range_carries_vary(tmp_path):
    # The identity range slice (client sent no acceptable encoding) for a
    # precompressed-enabled asset also carries `Vary: Accept-Encoding`.
    (tmp_path / "app.css").write_bytes(b"RAWBYTES")
    (tmp_path / "app.css.br").write_bytes(b"BR")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Range": "bytes=0-2"}))
    assert resp.status_code == 206
    assert "Content-Encoding" not in resp.headers
    assert resp.headers["Vary"] == "Accept-Encoding"
    assert resp.body == b"RAW"


@pytest.mark.asyncio
async def test_406_when_identity_rejected_and_no_variant(tmp_path):
    # No usable compressed sibling exists, and the client explicitly rejected
    # identity along with every coding (`identity;q=0, br;q=0, gzip;q=0`). Per
    # RFC 9110 Sec. 12.5.3 the raw asset is unacceptable, so respond 406 rather
    # than serving the uncompressed bytes the client refused.
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(
        _req("/s/app.css", {"Accept-Encoding": "identity;q=0, br;q=0, gzip;q=0"})
    )
    assert resp.status_code == 406


@pytest.mark.asyncio
async def test_identity_served_when_no_accept_encoding(tmp_path):
    # A missing Accept-Encoding expresses no preference, so identity stays
    # acceptable and the uncompressed asset is served with 200.
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css"))
    assert resp.status_code == 200
    assert resp.body == b"body{color:red}"
    assert "Content-Encoding" not in resp.headers


@pytest.mark.asyncio
async def test_identity_served_when_only_compression_rejected(tmp_path):
    # The client rejects br/gzip but does not exclude identity, so the
    # uncompressed asset is still acceptable (200), not 406.
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=True)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "br;q=0, gzip;q=0"}))
    assert resp.status_code == 200
    assert resp.body == b"body{color:red}"
    assert "Content-Encoding" not in resp.headers


@pytest.mark.asyncio
async def test_precompressed_false_ignores_identity_rejection(tmp_path):
    # With precompressed=False the handler does not content-negotiate encoding,
    # so an `identity;q=0` rejection must not trigger 406 - behavior is
    # unchanged from a plain static handler.
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", precompressed=False)
    resp = await sf.handle(_req("/s/app.css", {"Accept-Encoding": "identity;q=0"}))
    assert resp.status_code == 200
    assert resp.body == b"body{color:red}"
