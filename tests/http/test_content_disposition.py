"""Response.set_content_disposition — RFC 6266 attachment headers."""

from __future__ import annotations

import pytest

from veloce import FileResponse, Response


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
    # Only the RFC 5987 extended form is emitted - no lossy legacy slot.
    assert cd == "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
    assert 'filename="' not in cd
    assert cd.count("filename") == 1


def test_ascii_name_with_spaces_and_punctuation_preserved():
    resp = Response()
    resp.set_content_disposition("attachment", filename="my report (final).txt")
    cd = resp.headers["Content-Disposition"]
    # Spaces and parens are quoted-string members, so they survive verbatim
    # with no `_`/`?` mangling.
    assert cd == 'attachment; filename="my report (final).txt"'


def test_quote_and_backslash_escaped():
    resp = Response()
    resp.set_content_disposition("attachment", filename='a"b\\c.txt')
    cd = resp.headers["Content-Disposition"]
    # Backslash escaped first, then the double-quote (RFC 9110 escape order).
    assert cd == 'attachment; filename="a\\"b\\\\c.txt"'


def test_tab_is_quotable():
    resp = Response()
    resp.set_content_disposition("attachment", filename="a\tb.txt")
    cd = resp.headers["Content-Disposition"]
    # HTAB is a quoted-string member, so the name stays in the quoted slot.
    assert cd == 'attachment; filename="a\tb.txt"'
    assert "filename*=" not in cd


def test_pure_ascii_control_char_routes_to_extended():
    resp = Response()
    resp.set_content_disposition("attachment", filename="a\x01b.txt")
    cd = resp.headers["Content-Disposition"]
    # A non-CR/LF control char is not quotable, so the name routes to the
    # RFC 5987 extended form.
    assert cd == "attachment; filename*=UTF-8''a%01b.txt"
    assert 'filename="' not in cd


def test_returns_header_value():
    resp = Response()
    returned = resp.set_content_disposition("attachment", filename="x.txt")
    assert returned == resp.headers["Content-Disposition"]


def test_file_response_rejects_crlf_filename(tmp_path):
    f = tmp_path / "d.bin"
    f.write_bytes(b"x")
    # An embedded CR/LF in the filename is a header-injection attempt and is
    # rejected at the call site rather than silently sanitised.
    with pytest.raises(ValueError):
        FileResponse(str(f), filename='a"\r\nX-Injected: 1.txt')
