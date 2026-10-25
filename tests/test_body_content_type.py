"""A JSON body model reads a body the client declared as JSON.

The body was parsed as JSON whatever the `Content-Type` said, including
`text/plain`. That is not only taking a guess over a statement — it is a CSRF
avenue. `text/plain`, `multipart/form-data` and
`application/x-www-form-urlencoded` are the content types a cross-origin form or
`fetch` may send *without* a CORS preflight (the Fetch Standard's safelist), so
a JSON endpoint that accepts a body under `text/plain` can be driven
cross-origin through a cookie-authenticated victim's browser with nothing to
preflight against.

A missing header is refused on the same grounds. It was accepted, on the
grounds that a browser cannot omit it on a cross-origin send - and it can: a
`fetch` whose body is a `Blob` typed `""` sends no `Content-Type` at all, which
leaves no header to test against the safelist and so needs no preflight either.
Driven in Chrome against a cookie-authenticated endpoint the body arrived with
the header absent and was parsed, which is the one case the rule was documented
to make impossible. `force=True` reads such a body deliberately, which is where
a client that genuinely omits the header belongs. A `+json` structured suffix
(RFC 6839) is JSON.

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

from veloce import Body, Veloce
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

    @app.post("/manual")
    async def manual(request) -> dict:
        return {"seen": await request.json()}

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


# ── Refused ──────────────────────────────────────────────────────────


def test_an_absent_content_type_is_refused():
    """The hole the rule was documented to close, and did not."""
    assert _post(_client(), None).status_code == 422


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


# ── The same rule on the manual door ─────────────────────────────────
#
# The gate reached the body-model path and nothing else, so a handler that read
# its body with `await request.json()` still parsed a cross-origin `text/plain`
# send. Both doors now consult one predicate.


def _manual(client: TestClient, content_type: str | None, content: bytes = _BODY):
    headers = {"content-type": content_type} if content_type is not None else {}
    return client.post("/manual", content=content, headers=headers)


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_the_manual_door_refuses_the_cors_safelisted_types(content_type):
    """The defect: this door parsed the body a cross-origin form can send."""
    response = _manual(_client(), content_type)
    assert response.status_code == 200
    assert response.json()["seen"] is None


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/vnd.api+json", "APPLICATION/JSON"]
)
def test_the_manual_door_still_reads_a_json_body(content_type):
    assert _manual(_client(), content_type).json()["seen"] == {"name": "x", "qty": 1}


def test_the_manual_door_refuses_a_body_with_no_declared_type():
    """A browser can omit it cross-origin, which is why this is refused."""
    assert _manual(_client(), None).json()["seen"] is None


@pytest.mark.parametrize(
    "content_type",
    [None, "application/json", "application/vnd.api+json", "text/plain", "text/html"],
)
def test_both_doors_agree_on_every_content_type(content_type):
    """One rule: whatever a body model accepts, the manual read accepts."""
    client = _client()
    model_read = _post(client, content_type).status_code == 200
    manual_read = _manual(client, content_type).json()["seen"] is not None
    assert model_read == manual_read, content_type


def test_a_structured_suffix_outside_the_application_tree_is_refused_by_both():
    """`text/foo+json` is not JSON; the two doors used to disagree about it."""
    client = _client()
    assert _post(client, "text/foo+json").status_code == 422
    assert _manual(client, "text/foo+json").json()["seen"] is None


# ── The send that proved the absent header is reachable ──────────────


def test_the_blob_shaped_cross_origin_send_is_refused():
    """The exact request Chrome produced, replayed as the server saw it.

    `fetch(url, {method: "POST", mode: "no-cors", credentials: "include",
    body: new Blob([json], {type: ""})})` arrives with a JSON body, the
    victim's cookie, an `Origin` from the attacking page and no `Content-Type`
    at all. No header means nothing for the CORS safelist to reject, so no
    preflight is made - the body simply arrives.
    """
    client = _client()
    response = client.post(
        "/body",
        content=_BODY,
        headers={"origin": "http://attacker.example", "cookie": "session=victim"},
    )
    assert response.status_code == 422


def test_force_still_reads_a_body_with_no_declared_type():
    """The deliberate read, for a client that really does omit the header."""
    app = Veloce(openapi_url=None)

    @app.post("/forced")
    async def forced(request) -> dict:
        return {"seen": request.get_json(force=True)}

    with TestClient(app) as client:
        response = client.post("/forced", content=_BODY)
    assert response.json()["seen"] == {"name": "x", "qty": 1}


def test_a_scalar_body_names_the_refused_type_rather_than_a_missing_value():
    """The gate ran for a model body only, so a scalar one reported the wrong thing.

    `await request.json()` answers `None` for a refused type, and the
    missing-value branch then said "Missing required parameter" - which sends
    the caller looking for an absent field instead of a wrong header.
    """
    app = Veloce(openapi_url=None)

    @app.post("/scalar")
    async def scalar(value: int = Body()) -> dict:
        return {"value": value}

    with TestClient(app) as client:
        response = client.post("/scalar", content=b"5", headers={"content-type": "text/plain"})
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert "text/plain" in detail["msg"]
    assert "Missing" not in detail["msg"]
