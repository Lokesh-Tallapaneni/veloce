"""Request.is_multipart / is_form / content_encoding accessors."""

from __future__ import annotations

from veloce import Request


def _req(ct: str = "", encoding: str = "") -> Request:
    headers = {}
    if ct:
        headers["content-type"] = ct
    if encoding:
        headers["content-encoding"] = encoding
    return Request(method="POST", path="/x", query_string="", headers=headers, body=b"")


# ── is_multipart ─────────────────────────────────────────────────────


def test_is_multipart_form_data():
    assert _req("multipart/form-data; boundary=----X").is_multipart is True


def test_is_multipart_mixed():
    assert _req("multipart/mixed").is_multipart is True


def test_is_multipart_false_for_urlencoded():
    assert _req("application/x-www-form-urlencoded").is_multipart is False


def test_is_multipart_false_for_no_body():
    assert _req().is_multipart is False


# ── is_form ──────────────────────────────────────────────────────────


def test_is_form_urlencoded_true():
    assert _req("application/x-www-form-urlencoded").is_form is True


def test_is_form_multipart_true():
    assert _req("multipart/form-data; boundary=X").is_form is True


def test_is_form_json_false():
    assert _req("application/json").is_form is False


# ── content_encoding ─────────────────────────────────────────────────


def test_content_encoding_missing_returns_empty():
    assert _req().content_encoding == ""


def test_content_encoding_lowercased():
    assert _req(encoding="GZIP").content_encoding == "gzip"


def test_content_encoding_stripped():
    assert _req(encoding="  br  ").content_encoding == "br"


# ── multipart form parsing preserves duplicates ────────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


def _multipart_body(boundary: str, parts: list[tuple[str, str]]) -> bytes:
    """Build a minimal multipart body. parts = [(name, value), ...]."""
    lines = []
    for name, value in parts:
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{name}"')
        lines.append("")
        lines.append(value)
    lines.append(f"--{boundary}--")
    lines.append("")
    return "\r\n".join(lines).encode()


async def test_multipart_form_repeated_fields():
    boundary = "veloceboundary123"
    body = _multipart_body(
        boundary,
        [("tag", "a"), ("tag", "b"), ("name", "alice")],
    )
    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    form = await req.form()
    assert form.getlist("tag") == ["a", "b"]
    assert form["name"] == "alice"
