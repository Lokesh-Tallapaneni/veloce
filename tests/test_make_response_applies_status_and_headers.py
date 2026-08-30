"""`make_response(response, status, headers)` applies them, as the tuple form does.

The two spellings of the same intent disagreed:

    make_response(resp, 403, {"X-Reason": "denied"})    -> status 200, no header
    make_response((resp, 403, {"X-Reason": "denied"}))  -> status 403, header set

The early `if isinstance(body, Response): return body` returned before reaching
anything that applies `status_code`, `headers` or `content_type`. So the Flask
idiom the guide teaches - `return make_response(jsonify({"error": ...}), 403)` -
answered HTTP 200, and a client branching on the status read a denial as a
success.

Before that early return existed the same call produced a garbage JSON body
(the response object's `repr`), so it failed loudly. Adding the pass-through
fixed the body and turned the status loss silent.

This is the entry-point disagreement `_unpack_response_tuple` was introduced to
remove, in the same function, on the other spelling.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce, jsonify, make_response
from veloce.testclient import TestClient


def test_a_status_passed_beside_a_response_is_applied():
    """The regression: a denial answered 200."""
    assert make_response(Response(body=b"no"), 403).status_code == 403


def test_headers_passed_beside_a_response_are_applied():
    resp = make_response(Response(body=b"no"), 403, {"X-Reason": "denied"})

    assert resp.headers["X-Reason"] == "denied"


def test_the_two_spellings_agree():
    """Stated as one assertion, because the defect was the two disagreeing."""
    direct = make_response(Response(body=b"no"), 403, {"X-Reason": "denied"})
    tupled = make_response((Response(body=b"no"), 403, {"X-Reason": "denied"}))

    assert (direct.status_code, dict(direct.headers)) == (
        tupled.status_code,
        dict(tupled.headers),
    )


def test_a_response_passed_alone_keeps_its_own_status():
    """No status was supplied, so the default must not overwrite a real one."""
    assert make_response(Response(body=b"gone", status_code=404)).status_code == 404


def test_a_response_passed_alone_keeps_its_own_headers():
    original = Response(body=b"x", headers={"X-Kept": "1"})

    assert make_response(original).headers["X-Kept"] == "1"


def test_an_explicit_200_still_overrides():
    """`None` means unsupplied; a caller writing 200 means 200."""
    assert make_response(Response(body=b"x", status_code=404), 200).status_code == 200


def test_the_response_object_is_returned_not_a_copy():
    """The pass-through exists so a handler's own response survives intact."""
    original = Response(body=b"payload")

    assert make_response(original, 403) is original
    assert original.body == b"payload"


def test_the_guides_idiom_answers_the_status_it_names():
    """`docs/guide/helpers.md` teaches the two-argument shape; end to end."""
    app = Veloce(openapi_url=None)

    @app.get("/forbidden")
    async def forbidden():
        return make_response(jsonify({"error": "forbidden"}), 403)

    resp = TestClient(app).get("/forbidden")

    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


@pytest.mark.parametrize("status", [201, 400, 404, 500])
def test_every_status_reaches_the_response(status: int):
    assert make_response(Response(body=b"x"), status).status_code == status


def test_a_non_response_body_still_defaults_to_200():
    """The other branches must not acquire a `None` status."""
    assert make_response("hello").status_code == 200
    assert make_response({"a": 1}).status_code == 200
    assert make_response(b"raw").status_code == 200


def test_a_non_response_body_still_takes_an_explicit_status():
    assert make_response("hello", 201).status_code == 201
    assert make_response({"a": 1}, 201).status_code == 201
