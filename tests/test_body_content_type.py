"""A JSON body model reads a body the client declared as JSON.

The body was parsed as JSON whatever the `Content-Type` said, including
`text/plain`. That is not only taking a guess over a statement — it is a CSRF
avenue. `text/plain`, `multipart/form-data` and
`application/x-www-form-urlencoded` are the content types a cross-origin form or
`fetch` may send *without* a CORS preflight (the Fetch Standard's safelist), so
a JSON endpoint that accepts a body under `text/plain` can be driven
cross-origin through a cookie-authenticated victim's browser with nothing to
preflight against.

A missing header is still accepted: plenty of clients omit it and its absence
asserts nothing. A `+json` structured suffix (RFC 6839) is JSON.

Malformed JSON is the other half. `Request.on_json_loading_failed` raises
`BadRequest` under a deliberate policy — a stable message by default, the
decoder's reason only under `JSON_ERRORS_VERBOSE` or debug, because the offsets
come from attacker-controlled input. The body-model path swallowed that and
substituted a generic message, so the documented opt-in did nothing there and
the two ways of reading a body disagreed.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient

_BODY = b'{"name":"x","qty":1}'
_MALFORMED = b'{"name": "x", qty: 1}'


class Item(BaseModel):
    name: str
    qty: int


def _client(**config) -> TestClient:
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.post("/body")
    async def body(item: Item) -> dict:
        return {"got": item.name}

    @app.post("/raw")
    async def raw(request) -> dict:
        return await request.json()

    return TestClient(app)


def _post(client: TestClient, content_type: str | None, content: bytes = _BODY):
    headers = {"content-type": content_type} if content_type is not None else {}
    return client.post("/body", content=content, headers=headers)


# ── Accepted ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "APPLICATION/JSON",
        "application/json; charset=utf-8",
        "application/vnd.api+json",
        "application/ld+json",
    ],
)
def test_a_json_content_type_is_read(content_type):
    assert _post(_client(), content_type).status_code == 200


def test_an_absent_content_type_is_read():
    """Its absence asserts nothing, and plenty of clients omit it."""
    assert _post(_client(), None).status_code == 200


# ── Refused ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "application/xml",
        "text/html",
        "application/octet-stream",
    ],
)
def test_a_non_json_content_type_is_refused(content_type):
    assert _post(_client(), content_type).status_code == 422


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_the_cors_safelisted_types_are_refused(content_type):
    """These three are what a cross-origin request may send with no preflight."""
    response = _post(_client(), content_type)
    assert response.status_code == 422
    assert "JSON" in response.json()["detail"][0]["msg"]


def test_the_refusal_names_the_type_that_was_sent():
    detail = _post(_client(), "text/plain").json()["detail"][0]
    assert detail["loc"] == ["body"]
    assert "text/plain" in detail["msg"]


def test_the_body_is_not_parsed_when_the_type_is_refused():
    """Refused before the decoder runs, so a malformed body reports the type."""
    response = _post(_client(), "text/plain", content=b"not json at all")
    assert response.status_code == 422
    assert "text/plain" in response.json()["detail"][0]["msg"]


# ── The rest of the body contract is unchanged ───────────────────────


def test_a_well_formed_body_that_fails_validation_is_still_422():
    client = _client()
    response = client.post(
        "/body", content=b'{"name":"x"}', headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "qty"]


def test_an_empty_body_is_still_reported_as_missing():
    client = _client()
    response = client.post("/body", content=b"", headers={"content-type": "application/json"})
    assert response.status_code == 422


# ── Malformed JSON follows the documented policy ─────────────────────


def test_malformed_json_is_stable_by_default():
    """No decoder internals in the response: the offsets are attacker-derived."""
    client = _client()
    response = client.post(
        "/body", content=_MALFORMED, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON body"


def test_the_verbose_opt_in_reaches_a_body_model():
    """The defect: the flag worked for `request.json()` and not for a model."""
    client = _client(JSON_ERRORS_VERBOSE=True)
    response = client.post(
        "/body", content=_MALFORMED, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "column 15" in response.json()["detail"]


def test_debug_mode_also_reaches_a_body_model():
    client = _client(DEBUG=True)
    response = client.post(
        "/body", content=_MALFORMED, headers={"content-type": "application/json"}
    )
    assert "column 15" in response.json()["detail"]


@pytest.mark.parametrize("verbose", [False, True])
def test_both_ways_of_reading_a_body_agree(verbose):
    """One malformed body, one answer, however the handler declared it."""
    client = _client(JSON_ERRORS_VERBOSE=verbose)
    headers = {"content-type": "application/json"}
    via_model = client.post("/body", content=_MALFORMED, headers=headers)
    via_request = client.post("/raw", content=_MALFORMED, headers=headers)
    assert via_model.status_code == via_request.status_code == 400
    assert via_model.json()["detail"] == via_request.json()["detail"]
