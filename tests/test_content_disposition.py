"""Response.set_content_disposition — RFC 6266 attachment headers."""

from __future__ import annotations

from veloce import Response


def test_default_attachment():
    resp = Response()
    out = resp.set_content_disposition()
    assert out == "attachment"
    assert resp.headers["Content-Disposition"] == "attachment"


def test_attachment_with_filename():
    resp = Response()
    resp.set_content_disposition("attachment", filename="report.pdf")
    assert resp.headers["Content-Disposition"] == 'attachment; filename="report.pdf"'


def test_inline_disposition():
    resp = Response()
    resp.set_content_disposition("inline", filename="preview.png")
    cd = resp.headers["Content-Disposition"]
    assert cd.startswith("inline")
    assert 'filename="preview.png"' in cd


def test_non_ascii_filename_gets_rfc5987_form():
    resp = Response()
    resp.set_content_disposition("attachment", filename="résumé.pdf")
    cd = resp.headers["Content-Disposition"]
    # Both the ASCII fallback and the UTF-8 extended form are present.
    assert "filename=" in cd
    assert "filename*=UTF-8''" in cd


def test_returns_header_value():
    resp = Response()
    returned = resp.set_content_disposition("attachment", filename="x.txt")
    assert returned == resp.headers["Content-Disposition"]


def test_file_response_neutralises_unsafe_filename(tmp_path):
    from veloce import FileResponse

    f = tmp_path / "d.bin"
    f.write_bytes(b"x")
    resp = FileResponse(str(f), filename='a"\r\nX-Injected: 1.txt')
    cd = resp.headers["Content-Disposition"]
    assert "\r" not in cd and "\n" not in cd
    # The embedded double-quote is neutralised — only the wrapping quotes remain.
    assert cd.count('"') == 2
