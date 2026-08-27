"""Tests for Content-Disposition parsing in veloce.http.formparsers."""

from __future__ import annotations

from veloce import Request, TestClient, Veloce
from veloce.http.formparsers import _parse_content_disposition


def test_quoted_semicolon_preserved_in_name():
    disp, params = _parse_content_disposition('form-data; name="a;b"')
    assert disp == "form-data"
    assert params["name"] == "a;b"


def test_escaped_quote_inside_quoted_value():
    disp, params = _parse_content_disposition(r'form-data; name="a\"b"')
    assert disp == "form-data"
    assert params["name"] == 'a"b'


def test_escaped_backslash_inside_quoted_value():
    disp, params = _parse_content_disposition(r'form-data; name="a\\b"')
    assert disp == "form-data"
    assert params["name"] == "a\\b"


def test_unquoted_value_still_parses():
    disp, params = _parse_content_disposition("form-data; name=plain")
    assert disp == "form-data"
    assert params["name"] == "plain"


def test_whitespace_around_semicolon():
    disp, params = _parse_content_disposition('form-data; name="a" ; filename="report.pdf"')
    assert disp == "form-data"
    assert params["name"] == "a"
    assert params["filename"] == "report.pdf"


def test_parameter_keys_lowercased():
    _, params = _parse_content_disposition('form-data; Name="x"; FileName="y.txt"')
    assert params["name"] == "x"
    assert params["filename"] == "y.txt"


def test_quoted_semicolon_in_filename_with_following_param():
    disp, params = _parse_content_disposition('form-data; name="upload"; filename="weird;name.pdf"')
    assert disp == "form-data"
    assert params["name"] == "upload"
    assert params["filename"] == "weird;name.pdf"


# ── Per-part header capture into UploadFile.headers ──────────────────


def _file_part_body(boundary: str, extra_headers: dict[str, str]) -> bytes:
    lines = [f"--{boundary}"]
    lines.append('Content-Disposition: form-data; name="file"; filename="a.bin"')
    lines.append("Content-Type: application/octet-stream")
    for k, v in extra_headers.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("payload")
    lines.append(f"--{boundary}--")
    lines.append("")
    return "\r\n".join(lines).encode()


def test_uploadfile_captures_part_headers():
    boundary = "veloceboundary123"
    body = _file_part_body(boundary, {"Content-Transfer-Encoding": "binary", "X-Part-Id": "42"})
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    upload = form["file"]
    # Case-insensitive lookup proves the Headers view (parser stores lowercased).
    assert upload.headers["content-transfer-encoding"] == "binary"
    assert upload.headers["X-Part-Id"] == "42"
    assert upload.content_type == "application/octet-stream"


def test_uploadfile_headers_default_present_but_minimal():
    boundary = "veloceboundary123"
    body = _file_part_body(boundary, {})
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    upload = form["file"]
    # Only Content-Disposition + Content-Type were sent; .get with default works.
    assert upload.headers.get("x-missing", "d") == "d"


def test_uploadfile_headers_isolated_across_parts():
    boundary = "veloceboundary123"
    lines = [
        f"--{boundary}",
        'Content-Disposition: form-data; name="f1"; filename="a"',
        "X-Part-Id: one",
        "",
        "A",
        f"--{boundary}",
        'Content-Disposition: form-data; name="f2"; filename="b"',
        "X-Part-Id: two",
        "",
        "B",
        f"--{boundary}--",
        "",
    ]
    body = "\r\n".join(lines).encode()
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    assert form["f1"].headers["X-Part-Id"] == "one"
    assert form["f2"].headers["X-Part-Id"] == "two"


def test_uploadfile_accepts_plain_dict_headers():
    from veloce.http.datastructures import UploadFile

    up = UploadFile(filename="x", headers={"X-A": "1"})
    assert up.headers["x-a"] == "1"


# ── Helpers shared by the limit / boundary / charset tests ───────────

import pytest  # noqa: E402

from veloce.exceptions import BadRequest, RequestEntityTooLarge  # noqa: E402
from veloce.http.formparsers import parse_multipart_form  # noqa: E402

_BOUNDARY = "veloceboundary123"


def _field(name: str, value: str, *, content_type: str | None = None) -> list[str]:
    lines = [f"--{_BOUNDARY}", f'Content-Disposition: form-data; name="{name}"']
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    lines.append("")
    lines.append(value)
    return lines


def _file(name: str, filename: str, payload: str) -> list[str]:
    return [
        f"--{_BOUNDARY}",
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"',
        "Content-Type: application/octet-stream",
        "",
        payload,
    ]


def _assemble(*parts: list[str]) -> bytes:
    lines: list[str] = []
    for part in parts:
        lines.extend(part)
    lines.append(f"--{_BOUNDARY}--")
    lines.append("")
    return "\r\n".join(lines).encode()


def _ct() -> str:
    return f"multipart/form-data; boundary={_BOUNDARY}"


# ── Finding: missing / malformed boundary handling ──────────


def test_missing_boundary_raises_bad_request():
    body = b"--x--\r\n"
    with pytest.raises(BadRequest):
        parse_multipart_form(body, "multipart/form-data")


def test_malformed_boundary_too_long_raises_bad_request():
    long_boundary = "a" * 71
    body = b"--y--\r\n"
    with pytest.raises(BadRequest):
        parse_multipart_form(body, f"multipart/form-data; boundary={long_boundary}")


def test_malformed_boundary_illegal_char_raises_bad_request():
    with pytest.raises(BadRequest):
        parse_multipart_form(b"--z--\r\n", 'multipart/form-data; boundary="a\x01b"')


def test_valid_boundary_with_special_chars_parses():
    boundary = "a+b/c:d=e"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="x"\r\n\r\nv\r\n--{boundary}--\r\n'
    ).encode()
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    assert form["x"] == "v"


def test_malformed_body_mid_parse_raises_bad_request():
    # A part delimiter with a stray byte makes the underlying parser raise
    # mid-body; the partial form must be rejected with 400 rather than
    # returned as an incomplete 200, matching the malformed-boundary posture.
    body = (
        f"--{_BOUNDARY}X\r\n"
        'Content-Disposition: form-data; name="a"\r\n\r\n'
        f"value-a\r\n--{_BOUNDARY}--\r\n"
    ).encode()
    with pytest.raises(BadRequest):
        parse_multipart_form(body, _ct())


# ── Finding: separate field/file limits + field memory ─


def test_max_fields_caps_text_field_count():
    body = _assemble(_field("a", "1"), _field("b", "2"), _field("c", "3"))
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, _ct(), max_fields=2)


def test_max_files_caps_file_count_only():
    body = _assemble(
        _field("t1", "x"),
        _field("t2", "y"),
        _file("f1", "a.bin", "AAAA"),
        _file("f2", "b.bin", "BBBB"),
    )
    # Two files exceed max_files=1 even though there are also two text fields.
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, _ct(), max_files=1)


def test_field_and_file_counts_are_independent():
    body = _assemble(
        _field("t1", "x"),
        _field("t2", "y"),
        _file("f1", "a.bin", "AAAA"),
    )
    # 2 fields / 1 file: allowed when each cap is generous enough.
    form = parse_multipart_form(body, _ct(), max_fields=2, max_files=1)
    assert form["t1"] == "x"
    assert form["f1"].filename == "a.bin"


def test_max_field_size_caps_text_not_files():
    body = _assemble(_field("small", "ok"), _file("big", "big.bin", "X" * 5000))
    # A 2-byte field passes while a 5000-byte file is permitted by a larger
    # file cap, proving the two size limits are independent.
    form = parse_multipart_form(body, _ct(), max_field_size=10, max_file_size=10000)
    assert form["small"] == "ok"
    assert form["big"].size == 5000


def test_max_field_size_rejects_oversized_field():
    body = _assemble(_field("f", "X" * 100))
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, _ct(), max_field_size=10)


def test_max_field_memory_caps_cumulative_text_bytes():
    body = _assemble(_field("a", "X" * 30), _field("b", "Y" * 30))
    # Each field is under any per-field cap, but their sum exceeds the
    # cumulative resident-memory ceiling.
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, _ct(), max_field_memory=40)


def test_max_field_memory_excludes_files():
    body = _assemble(_field("a", "X" * 10), _file("f", "f.bin", "Z" * 1000))
    # File bytes do not count toward the field-memory ceiling.
    form = parse_multipart_form(body, _ct(), max_field_memory=100)
    assert form["a"] == "X" * 10
    assert form["f"].size == 1000


def test_part_size_alias_still_applies_to_both():
    body = _assemble(_field("f", "X" * 100))
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, _ct(), max_part_size=10)


# ── Finding: per-part Content-Type charset (Werkzeug) ────────────────


def test_part_charset_iso_8859_1_decodes_field():
    boundary = _BOUNDARY
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "Content-Type: text/plain; charset=iso-8859-1\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + b"\xe9" + tail
    form = parse_multipart_form(body, _ct())
    assert form["name"] == "\xe9"


def test_part_charset_overrides_global_fallback():
    boundary = _BOUNDARY
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "Content-Type: text/plain; charset=iso-8859-1\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + b"\xe9" + tail
    # Even with no global fallback, the part's own charset is honored.
    form = parse_multipart_form(body, _ct(), charset_fallback=None)
    assert form["name"] == "\xe9"


def test_unsupported_part_charset_raises_bad_request():
    body = _assemble(_field("n", "v", content_type="text/plain; charset=shift_jis"))
    with pytest.raises(BadRequest):
        parse_multipart_form(body, _ct())


def test_no_part_charset_falls_back_to_global():
    boundary = _BOUNDARY
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n').encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + b"\xe9" + tail
    # No part charset declared: non-UTF-8 bytes are rejected by default.
    with pytest.raises(BadRequest):
        parse_multipart_form(body, _ct())
    # ...but the global latin-1 fallback still applies.
    form = parse_multipart_form(body, _ct(), charset_fallback="latin-1")
    assert form["name"] == "\xe9"


def test_declared_utf8_charset_rejects_invalid_bytes():
    # A part that declares charset=utf-8 asserts its bytes are valid UTF-8;
    # an invalid lead byte must be a 400, not U+FFFD-corrupted text.
    boundary = _BOUNDARY
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + b"\xff" + tail
    with pytest.raises(BadRequest):
        parse_multipart_form(body, _ct())


def test_declared_ascii_charset_rejects_high_byte():
    # charset=ascii with a byte > 0x7f is out of range and must be rejected.
    boundary = _BOUNDARY
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "Content-Type: text/plain; charset=ascii\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + b"\x80" + tail
    with pytest.raises(BadRequest):
        parse_multipart_form(body, _ct())


def test_declared_charset_valid_bytes_still_decode():
    # A declared charset with matching bytes must still decode normally - the
    # strict path rejects only genuinely invalid bytes.
    boundary = _BOUNDARY
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + "café".encode() + tail
    form = parse_multipart_form(body, _ct())
    assert form["name"] == "café"


# ── A truncated body is refused, not silently accepted ───────────────

_TRUNC_BOUNDARY = "----truncation-probe"
_TRUNC_HEADERS = {"Content-Type": f"multipart/form-data; boundary={_TRUNC_BOUNDARY}"}


def _multipart(*fields: tuple[str, str], terminator: bool = True) -> bytes:
    parts = "".join(
        f'--{_TRUNC_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in fields
    )
    return (parts + (f"--{_TRUNC_BOUNDARY}--\r\n" if terminator else "")).encode()


def _echo_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/upload")
    async def upload(request: Request):
        form = await request.form()
        return {"fields": {k: v for k, v in form.items() if isinstance(v, str)}}

    return app


def test_a_body_truncated_mid_part_is_refused():
    """The parser reports no error for a short body, so the field just vanished.

    A truncated upload returned 200 with the cut part's field silently missing,
    which a caller cannot tell from a form that genuinely omitted it.
    """
    complete = _multipart(("a", "value-a"), ("b", "value-b"))
    truncated = complete[: complete.index(b"value-b")]
    with TestClient(_echo_app()) as client:
        response = client.post("/upload", content=truncated, headers=_TRUNC_HEADERS)
    assert response.status_code == 400
    assert "truncated" in response.json()["detail"]


def test_a_body_truncated_in_a_part_header_is_refused():
    complete = _multipart(("a", "value-a"), ("b", "value-b"))
    with TestClient(_echo_app()) as client:
        response = client.post("/upload", content=complete[:60], headers=_TRUNC_HEADERS)
    assert response.status_code == 400


def test_a_body_missing_its_closing_delimiter_is_refused():
    """The final part never ends, which is the same truncation."""
    with TestClient(_echo_app()) as client:
        response = client.post(
            "/upload", content=_multipart(("a", "1"), terminator=False), headers=_TRUNC_HEADERS
        )
    assert response.status_code == 400


def test_a_complete_body_is_still_accepted():
    with TestClient(_echo_app()) as client:
        response = client.post(
            "/upload", content=_multipart(("a", "1"), ("b", "2")), headers=_TRUNC_HEADERS
        )
    assert response.status_code == 200
    assert response.json()["fields"] == {"a": "1", "b": "2"}


def test_an_empty_form_is_still_accepted():
    """A terminator with no parts is a valid empty form, not a truncation."""
    with TestClient(_echo_app()) as client:
        response = client.post(
            "/upload", content=f"--{_TRUNC_BOUNDARY}--\r\n".encode(), headers=_TRUNC_HEADERS
        )
    assert response.status_code == 200
    assert response.json()["fields"] == {}


def test_an_empty_field_value_is_still_accepted():
    with TestClient(_echo_app()) as client:
        response = client.post("/upload", content=_multipart(("a", "")), headers=_TRUNC_HEADERS)
    assert response.status_code == 200
    assert response.json()["fields"] == {"a": ""}
