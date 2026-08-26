"""One RFC 9110 quoted-string escape, used by every producer of one.

The transform - escape the backslash, *then* the double-quote - was written out
in four modules. Three had it right; `Headers.add` had only the double-quote
half, and its trigger set did not treat a backslash as needing quoting at all.
Both halves were observable:

    Headers().add("Content-Disposition", "attachment", filename="ends with \\")
    -> attachment; filename="ends with \\"

The closing quote is consumed as a quoted-pair, so the parameter is an
unterminated quoted-string. And a value like `a\b` was emitted as a bare token,
though a backslash is not a `tchar` (RFC 9110 Sec. 5.6.2).

The four copies are now one `_quote_header_value` in `_internal.py` - the
module documented as the cross-subpackage carve-out - and these tests check the
producers against a strict reader rather than against the escape's own spelling.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.http.datastructures import Headers
from veloce.testclient import TestClient

BACKSLASH = chr(92)

AWKWARD = [
    "plain",
    "with space",
    'has"quote',
    "has" + BACKSLASH + "backslash",
    "ends with" + BACKSLASH,
    BACKSLASH + "leads",
    BACKSLASH * 2,
    "both" + BACKSLASH + '"',
    "semi;colon",
    "com,ma",
]


def read_quoted(raw: str) -> tuple[str, bool]:
    """Read a quoted-string the way RFC 9110 Sec. 5.6.4 says to.

    Returns the decoded value and whether the string was terminated. Written
    here rather than reusing an `email` parser because those are lenient about
    exactly the malformation being tested.
    """
    assert raw.startswith('"'), raw
    out: list[str] = []
    index = 1
    while index < len(raw):
        char = raw[index]
        if char == BACKSLASH and index + 1 < len(raw):
            out.append(raw[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(out), True
        out.append(char)
        index += 1
    return "".join(out), False


def _param_of(header: str) -> str:
    return header.split("=", 1)[1]


# ── the reader itself is not vacuous ─────────────────────────────────


def test_the_reader_rejects_an_unterminated_string():
    """If this passed, every assertion below would be meaningless."""
    _, terminated = read_quoted('"ends with' + BACKSLASH + '"')
    assert terminated is False


def test_the_reader_decodes_an_escaped_quote():
    assert read_quoted('"a' + BACKSLASH + '"b"') == ('a"b', True)


# ── Headers.add ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value", AWKWARD)
def test_a_header_parameter_round_trips(value):
    """The defect: a trailing backslash produced an unterminated parameter."""
    headers = Headers()
    headers.add("Content-Disposition", "attachment", filename=value)
    param = _param_of(headers["Content-Disposition"])
    if not param.startswith('"'):
        # An unquoted token is only legitimate when the value needs no quoting.
        assert param == value
        return
    assert read_quoted(param) == (value, True)


def test_a_backslash_forces_quoting():
    """A backslash is not a `tchar`, so it cannot be sent as a bare token."""
    headers = Headers()
    headers.add("Content-Disposition", "attachment", filename="a" + BACKSLASH + "b")
    assert _param_of(headers["Content-Disposition"]).startswith('"')


def test_a_plain_value_is_not_quoted():
    """The negative: quoting everything would pass every round-trip above."""
    headers = Headers()
    headers.add("Content-Disposition", "attachment", filename="plain")
    assert _param_of(headers["Content-Disposition"]) == "plain"


# ── Content-Disposition on a file response ───────────────────────────


@pytest.mark.parametrize("value", [v for v in AWKWARD if v.isascii()])
def test_an_attachment_filename_round_trips(tmp_path, value):
    path = tmp_path / "f.txt"
    path.write_text("body")

    app = Veloce(openapi_url=None)

    @app.get("/f")
    async def f():
        from veloce import FileResponse

        return await FileResponse.from_path(str(path), filename=value)

    disposition = TestClient(app).get("/f").headers["content-disposition"]
    param = _param_of(disposition)
    if param.startswith('"'):
        assert read_quoted(param) == (value, True)


# ── the test client's multipart headers ──────────────────────────────


@pytest.mark.parametrize("value", [v for v in AWKWARD if v.isascii() and "\n" not in v])
def test_a_multipart_filename_round_trips(value):
    app = Veloce(openapi_url=None)
    seen: dict = {}

    @app.post("/u")
    async def upload(request):
        form = await request.form()
        upload_file = form["f"]
        seen["filename"] = upload_file.filename
        return {"ok": True}

    client = TestClient(app)
    client.post("/u", files={"f": (value, b"data", "text/plain")})
    assert seen["filename"] == value


# ── all producers agree ──────────────────────────────────────────────


@pytest.mark.parametrize("value", AWKWARD)
def test_every_producer_escapes_identically(value):
    """The property the four copies existed to satisfy and did not."""
    from veloce._internal import _quote_header_value

    escaped = _quote_header_value(value)
    assert read_quoted('"' + escaped + '"') == (value, True)


def test_the_security_alias_is_the_same_function():
    """`security/_utils` re-exports it rather than keeping a fourth copy."""
    from veloce._internal import _quote_header_value
    from veloce.security._utils import _quote_header_value as security_copy

    assert security_copy is _quote_header_value


def test_a_realm_with_a_backslash_round_trips():
    """The WWW-Authenticate consumer, through the alias."""
    from veloce.security._utils import _quote_header_value

    realm = "corp" + BACKSLASH + '"prod'
    assert read_quoted('"' + _quote_header_value(realm) + '"') == (realm, True)
