"""Request.max_content_length body-size cap accessor."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.testclient import TestClient


def test_none_when_no_app_bound():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.max_content_length is None


def test_default_when_config_unset():
    app = Veloce()
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    # MAX_CONTENT_LENGTH defaults to 100 MiB in the seeded config.
    assert req.max_content_length == 100 * 1024 * 1024


def test_reads_configured_limit():
    app = Veloce()
    app.config["MAX_CONTENT_LENGTH"] = 1024
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    assert req.max_content_length == 1024


def test_reflects_config_changes():
    app = Veloce()
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    assert req.max_content_length == 100 * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = 4096
    assert req.max_content_length == 4096


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


# ── multipart DoS caps ─────────────────────────────────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


def test_multipart_part_count_cap_rejects():
    """A form with more parts than `max_parts` raises RequestEntityTooLarge."""
    from veloce.exceptions import RequestEntityTooLarge
    from veloce.http.formparsers import parse_multipart_form

    boundary = "veloceboundary123"
    body = _multipart_body(boundary, [(f"f{i}", "v") for i in range(10)])
    ct = f"multipart/form-data; boundary={boundary}"
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, ct, max_parts=3)


def test_multipart_part_size_cap_rejects():
    """A part whose body exceeds `max_part_size` raises RequestEntityTooLarge."""
    from veloce.exceptions import RequestEntityTooLarge
    from veloce.http.formparsers import parse_multipart_form

    boundary = "veloceboundary123"
    body = _multipart_body(boundary, [("big", "x" * 5000)])
    ct = f"multipart/form-data; boundary={boundary}"
    with pytest.raises(RequestEntityTooLarge):
        parse_multipart_form(body, ct, max_part_size=1000)


def test_multipart_within_caps_parses_normally():
    from veloce.http.formparsers import parse_multipart_form

    boundary = "veloceboundary123"
    body = _multipart_body(boundary, [("a", "1"), ("b", "2")])
    ct = f"multipart/form-data; boundary={boundary}"
    form = parse_multipart_form(body, ct, max_parts=10, max_part_size=1000)
    assert form["a"] == "1"
    assert form["b"] == "2"


def test_multipart_part_count_cap_from_app_config():
    """The `MAX_FORM_PARTS` config key drives the per-app part cap that a
    live request hits through `request.form()`."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 2

    @app.post("/u")
    async def u(request: Request):
        await request.form()
        return {"ok": True}

    boundary = "veloceboundary123"
    body = _multipart_body(boundary, [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])
    resp = TestClient(app).post(
        "/u",
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 413


def test_multipart_part_count_cap_zero_rejects_any_part():
    """`MAX_FORM_PARTS=0` is a deliberate value, not a missing config — a
    single part must trip the cap rather than silently falling back to
    the default 1000-part limit."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 0

    @app.post("/u")
    async def u(request: Request):
        await request.form()
        return {"ok": True}

    boundary = "veloceboundary123"
    body = _multipart_body(boundary, [("a", "1")])
    resp = TestClient(app).post(
        "/u",
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 413
