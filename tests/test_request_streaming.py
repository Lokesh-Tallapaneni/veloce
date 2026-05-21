"""F3 — request-body streaming and disk-spilling uploads.

`request.stream()` yields the body in bounded chunks, and a multipart
file part streams into a `SpooledTemporaryFile` — it stays in memory
while small and rolls over to a real temp file on disk once large, so a
big upload never sits fully in RAM.
"""

from __future__ import annotations

import tempfile

from veloce import Request, UploadFile, Veloce


def make_request(body: bytes = b"", headers: dict | None = None) -> Request:
    return Request(
        method="POST",
        path="/",
        query_string="",
        headers=headers or {},
        body=body,
    )


# ── request.stream() yields bounded chunks ────────────────────────────


async def test_stream_yields_bounded_chunks():
    big = b"x" * 200_000  # spans several 64 KiB chunks
    chunks = [chunk async for chunk in make_request(body=big).stream()]
    assert len(chunks) >= 3
    assert all(len(c) <= 65536 for c in chunks)
    assert b"".join(chunks) == big


async def test_stream_small_body_is_one_chunk():
    chunks = [chunk async for chunk in make_request(body=b"small").stream()]
    assert chunks == [b"small"]


async def test_stream_empty_body_yields_nothing():
    chunks = [chunk async for chunk in make_request(body=b"").stream()]
    assert chunks == []


# ── multipart uploads spill to disk ───────────────────────────────────


def _capture_upload_app() -> tuple[Veloce, dict]:
    app = Veloce(debug=True, openapi_url=None)
    captured: dict = {}

    @app.post("/u")
    async def upload(request: Request):
        form = await request.form()
        uploaded = form.get("file")
        assert isinstance(uploaded, UploadFile)
        captured["file_obj"] = uploaded.file
        captured["content"] = uploaded.content
        captured["size"] = uploaded.size
        captured["filename"] = uploaded.filename
        return {"ok": True}

    return app, captured


def test_uploaded_file_is_backed_by_a_spooled_file():
    app, captured = _capture_upload_app()
    app.test_client().post("/u", files={"file": ("a.txt", b"hello there")})

    assert isinstance(captured["file_obj"], tempfile.SpooledTemporaryFile)
    assert captured["content"] == b"hello there"
    assert captured["size"] == len(b"hello there")
    assert captured["filename"] == "a.txt"


def test_small_upload_stays_in_memory():
    app, captured = _capture_upload_app()
    app.test_client().post("/u", files={"file": ("small.bin", b"tiny")})

    # `_rolled` is SpooledTemporaryFile's rollover flag — False means the
    # data is still held in memory, not on disk.
    assert captured["file_obj"]._rolled is False


def test_large_upload_rolls_over_to_disk():
    app, captured = _capture_upload_app()
    big = b"A" * (2 * 1024 * 1024)  # 2 MiB — past the 1 MiB spool threshold
    app.test_client().post("/u", files={"file": ("big.bin", big)})

    # Rolled over: the data spilled to a real temp file rather than RAM.
    assert captured["file_obj"]._rolled is True
    # ...and the content survived the spill intact.
    assert captured["content"] == big
    assert captured["size"] == len(big)


def test_regular_form_field_still_parses_alongside_a_file():
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/m")
    async def mixed(request: Request):
        form = await request.form()
        return {
            "field": form.get("caption"),
            "file": form.get("file").filename,
        }

    client = app.test_client()
    resp = client.post(
        "/m",
        data={"caption": "a photo"},
        files={"file": ("pic.jpg", b"\xff\xd8\xff binary")},
    )
    assert resp.json() == {"field": "a photo", "file": "pic.jpg"}


def test_oversized_file_part_is_rejected_with_413():
    """The per-part size cap still fires for a file part with the new
    spool-write-then-check ordering."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["MAX_FORM_PART_SIZE"] = 1024  # 1 KiB per-part cap

    @app.post("/u")
    async def upload(request: Request):
        await request.form()
        return {"ok": True}

    resp = app.test_client().post("/u", files={"file": ("big.bin", b"A" * 5000)})
    assert resp.status_code == 413


def test_upload_save_works_after_rollover(tmp_path):
    """A rolled-over upload still saves correctly via UploadFile.save()."""
    app = Veloce(debug=True, openapi_url=None)
    dest = tmp_path / "saved.bin"
    big = b"Z" * (2 * 1024 * 1024)

    @app.post("/s")
    async def save(request: Request):
        form = await request.form()
        form.get("file").save(str(dest))
        return {"ok": True}

    app.test_client().post("/s", files={"file": ("big.bin", big)})
    assert dest.read_bytes() == big
