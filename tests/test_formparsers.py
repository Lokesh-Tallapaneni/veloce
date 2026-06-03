"""Tests for Content-Disposition parsing in veloce.http.formparsers."""

from __future__ import annotations

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
    from veloce.http.datastructures import parse_multipart_form

    boundary = "veloceboundary123"
    body = _file_part_body(boundary, {"Content-Transfer-Encoding": "binary", "X-Part-Id": "42"})
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    upload = form["file"]
    # Case-insensitive lookup proves the Headers view (parser stores lowercased).
    assert upload.headers["content-transfer-encoding"] == "binary"
    assert upload.headers["X-Part-Id"] == "42"
    assert upload.content_type == "application/octet-stream"


def test_uploadfile_headers_default_present_but_minimal():
    from veloce.http.datastructures import parse_multipart_form

    boundary = "veloceboundary123"
    body = _file_part_body(boundary, {})
    form = parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")
    upload = form["file"]
    # Only Content-Disposition + Content-Type were sent; .get with default works.
    assert upload.headers.get("x-missing", "d") == "d"


def test_uploadfile_headers_isolated_across_parts():
    from veloce.http.datastructures import parse_multipart_form

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
