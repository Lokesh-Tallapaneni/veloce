"""`request.get_json()` reports a content-type mismatch instead of going quiet.

A body declared as `text/plain` was already refused on every JSON door, but the
sync accessor signalled the refusal by returning `None` - which a caller cannot
tell apart from an empty body. A handler that subscripted the result raised a
`TypeError` and the caller got a 500 about the handler rather than a 415 about
their request. The refusal is unchanged - only now it is stated, as
`UnsupportedMediaType` raised through the `on_json_loading_failed` override
point. RFC 9110 Sec. 15.5.16: 415 is the status for a body whose media type the
target does not accept.

Malformed JSON under a correct content type stays `BadRequest` (400), so the two
failures remain distinguishable.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.exceptions import BadRequest, UnsupportedMediaType
from veloce.testclient import TestClient

_JSON = b'{"name": "ada"}'


def _body_request(body: bytes = _JSON, content_type: str | None = "application/json") -> Request:
    headers = {"content-type": content_type} if content_type else {}
    return make_request(method="POST", path="/items", headers=headers, body=body)


# ── A declared non-JSON content type is reported ─────────────────────

_REFUSED = [
    "text/plain",
    "text/plain; charset=utf-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
]


@pytest.mark.parametrize("content_type", _REFUSED)
def test_declared_non_json_raises_unsupported_media_type(content_type):
    with pytest.raises(UnsupportedMediaType) as exc:
        _body_request(content_type=content_type).get_json()
    assert exc.value.status_code == 415


def test_the_detail_names_the_offending_content_type():
    with pytest.raises(UnsupportedMediaType) as exc:
        _body_request(content_type="text/plain").get_json()
    assert "text/plain" in exc.value.detail


@pytest.mark.parametrize("content_type", _REFUSED)
def test_route_answers_415(content_type):
    app = Veloce(openapi_url=None)

    @app.post("/items")
    async def create(request: Request):
        data = request.get_json()
        return {"name": data["name"]}

    with TestClient(app) as client:
        response = client.post("/items", content=_JSON, headers={"content-type": content_type})
    assert response.status_code == 415


# ── A JSON declaration still parses ──────────────────────────────────


def test_application_json_parses():
    assert _body_request().get_json() == {"name": "ada"}


def test_structured_json_suffix_parses():
    # RFC 6839 Sec. 3.1: `+json` marks the body as JSON-encoded.
    request = _body_request(content_type="application/vnd.api+json")
    assert request.get_json() == {"name": "ada"}


def test_absent_content_type_raises_unsupported_media_type():
    # An absent header is a refusal too. It was allowed on the grounds that a
    # browser cannot omit it cross-origin; a `fetch` with a `Blob` body typed
    # `""` does exactly that, with no preflight, so the allowance was the one
    # hole in the rule. `force=True` below reads such a body deliberately.
    with pytest.raises(UnsupportedMediaType):
        _body_request(content_type=None).get_json()


# ── The escape hatches survive ───────────────────────────────────────


def test_force_parses_a_refused_content_type():
    request = _body_request(content_type="text/plain")
    assert request.get_json(force=True) == {"name": "ada"}


def test_silent_returns_none_instead_of_raising():
    request = _body_request(content_type="text/plain")
    assert request.get_json(silent=True) is None


def test_empty_body_returns_none():
    assert _body_request(body=b"").get_json() is None


def test_empty_body_without_a_content_type_still_raises():
    # Same ordering as the test below: the content-type decision runs before the
    # body is looked at, so its length does not change the answer.
    with pytest.raises(UnsupportedMediaType):
        _body_request(body=b"", content_type=None).get_json()


def test_empty_body_under_a_refused_content_type_still_raises():
    # The content-type decision runs before the body is looked at: the caller
    # asked for JSON and the request declared it is not, whatever its length.
    with pytest.raises(UnsupportedMediaType):
        _body_request(body=b"", content_type="text/plain").get_json()


# ── The override point still owns the policy ─────────────────────────


def test_subclass_override_can_change_the_outcome():
    class TolerantRequest(Request):
        def on_json_loading_failed(self, error):
            return {"fallback": True}

    request = TolerantRequest(
        method="POST",
        path="/items",
        query_string="",
        headers={"content-type": "text/plain"},
        body=_JSON,
    )
    assert request.get_json() == {"fallback": True}


def test_the_hook_receives_none_for_a_content_type_refusal():
    captured: list[Exception | None] = []

    class CapturingRequest(Request):
        def on_json_loading_failed(self, error):
            captured.append(error)
            return None

    request = CapturingRequest(
        method="POST",
        path="/items",
        query_string="",
        headers={"content-type": "text/plain"},
        body=_JSON,
    )
    assert request.get_json() is None
    assert captured == [None]


# ── The two failures stay distinguishable ────────────────────────────


def test_malformed_json_under_a_json_content_type_is_still_400():
    with pytest.raises(BadRequest) as exc:
        _body_request(body=b"{not json").get_json()
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid JSON body"


def test_malformed_json_route_answers_400_not_415():
    app = Veloce(openapi_url=None)

    @app.post("/items")
    async def create(request: Request):
        return {"name": request.get_json()["name"]}

    with TestClient(app) as client:
        response = client.post(
            "/items",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400
