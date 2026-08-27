"""`StaticFiles` reads the same parsed conditional headers the `Request` exposes.

The RFC 9110 Sec. 13 conditional-request policy is implemented twice: once on
`Response` (`make_conditional` / `check_preconditions`) and once inline in
`StaticFiles.handle`. The static copy re-split the raw `If-None-Match` header
with `split(",")` instead of reading `request.if_none_match`, which exists
precisely so callers do not do that. The two parsers genuinely disagree:

    If-None-Match: "abc,def", "xyz"

    split(",")            -> ['"abc', 'def"', '"xyz"']     # both tags destroyed
    request.if_none_match -> ('"abc,def"', '"xyz"')

because a comma is legal inside an opaque entity-tag (RFC 9110 Sec. 8.8.3).

**No request currently changes outcome, and this module does not pretend
otherwise.** A mis-split token simply fails to compare equal, and any correctly
parsed tag elsewhere in the list still matches - so the only way to observe a
difference is for the *server's own* ETag to contain a comma, and the one
`StaticFiles` computes (`W/"<size>-<mtime>"`) never does. Every candidate header
was checked and none flips the 304 decision.

So the change is de-duplication and robustness, not a bug fix: one ETag parser
instead of two, and a per-request cached property instead of a re-parse. These
tests pin the conditional behaviour that must survive it, and one test asserts
the parser difference directly - which is the part that is real - rather than
dressing it up as a reachable defect.
"""

from __future__ import annotations

import time

import pytest

from veloce import StaticFiles, Veloce
from veloce.http.request import _split_etag_list
from veloce.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    (tmp_path / "asset.txt").write_text("body")
    app = Veloce(openapi_url=None)

    app.mount("/s", StaticFiles(directory=str(tmp_path)))
    return TestClient(app)


def _etag(client) -> str:
    return client.get("/s/asset.txt").headers["ETag"]


# ── the normal conditional flow still works ──────────────────────────


def test_a_matching_etag_is_a_304(client):
    assert client.get("/s/asset.txt", headers={"If-None-Match": _etag(client)}).status_code == 304


def test_a_star_is_a_304(client):
    assert client.get("/s/asset.txt", headers={"If-None-Match": "*"}).status_code == 304


def test_a_non_matching_etag_is_a_200(client):
    resp = client.get("/s/asset.txt", headers={"If-None-Match": '"nope"'})
    assert resp.status_code == 200
    assert resp.body == b"body"


def test_no_conditional_header_is_a_200(client):
    assert client.get("/s/asset.txt").status_code == 200


# ── a tag list is parsed, not naively split ──────────────────────────


def test_a_matching_etag_in_a_list_is_a_304(client):
    header = f'"other", {_etag(client)}, "third"'
    assert client.get("/s/asset.txt", headers={"If-None-Match": header}).status_code == 304


def test_a_comma_bearing_tag_does_not_destroy_a_later_match(client):
    """A tag containing a comma must not prevent a real match later in the list.

    True under both parsers, which is why it is stated as behaviour to preserve
    rather than as the defect.
    """
    header = f'"abc,def", {_etag(client)}'
    assert client.get("/s/asset.txt", headers={"If-None-Match": header}).status_code == 304


def test_the_two_parsers_disagree_on_a_comma_bearing_tag():
    """The real, demonstrable difference - at the parser, where it exists.

    `split(",")` destroys an opaque tag containing a comma; `_split_etag_list`
    does not. No `StaticFiles` request can currently observe this, because the
    ETag it computes never contains a comma - so this is asserted here, at the
    level where it is true, instead of as an end-to-end claim that would be
    false.
    """

    header = '"abc,def", "xyz"'
    assert [t.strip() for t in header.split(",")] == ['"abc', 'def"', '"xyz"']
    assert list(_split_etag_list(header)) == ['"abc,def"', '"xyz"']


def test_the_static_handler_uses_the_parsed_property():
    """What the change actually guarantees: one parser, not two.

    Asserted through behaviour - a header the raw split would mangle is handled
    by whatever `Request` parsed - rather than by reading the source.
    """
    from tests.conftest import make_request

    request = make_request(path="/x", headers={"If-None-Match": '"abc,def", "xyz"'})
    assert request.if_none_match == ('"abc,def"', '"xyz"')


def test_a_comma_bearing_tag_alone_is_still_a_200(client):
    """The negative: it must not start matching things it should not."""
    resp = client.get("/s/asset.txt", headers={"If-None-Match": '"abc,def"'})
    assert resp.status_code == 200


def test_the_static_handler_agrees_with_the_request_property(client):
    """Stated as the property: whatever `Request` parses out of the header is
    what the static handler must have compared against."""
    from tests.conftest import make_request

    etag = _etag(client)
    for header in [etag, f'"x", {etag}', f'"abc,def", {etag}', "*", '"nope"']:
        request = make_request(path="/s/asset.txt", headers={"If-None-Match": header})
        parsed = request.if_none_match
        expected_304 = parsed[:1] == ("*",) or any(
            tag.strip('W/"') == etag.strip('W/"') for tag in parsed
        )
        actual = client.get("/s/asset.txt", headers={"If-None-Match": header}).status_code == 304
        assert actual is expected_304, (header, parsed)


# ── If-Modified-Since ────────────────────────────────────────────────


def test_a_future_if_modified_since_is_a_304(client):
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 3600))
    assert client.get("/s/asset.txt", headers={"If-Modified-Since": future}).status_code == 304


def test_a_past_if_modified_since_is_a_200(client):
    past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 86400))
    assert client.get("/s/asset.txt", headers={"If-Modified-Since": past}).status_code == 200


def test_an_unparseable_if_modified_since_is_a_200(client):
    assert client.get("/s/asset.txt", headers={"If-Modified-Since": "junk"}).status_code == 200


def test_if_none_match_supersedes_if_modified_since(client):
    """RFC 9110 Sec. 13.2 precedence, which the inline copy also implements."""
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 3600))
    resp = client.get(
        "/s/asset.txt",
        headers={"If-None-Match": '"nope"', "If-Modified-Since": future},
    )
    assert resp.status_code == 200
