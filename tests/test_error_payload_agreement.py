"""Every path that renders an `HTTPException` renders it identically.

`app/errors.py::http_exception_payload` was extracted so the two in-tree emit
paths - the request cycle and the out-of-band `handle_http_exception` - would
stop disagreeing. Its docstring says so. But the public, `__all__`-exported,
documented `exceptions.http_exception_handler` was a third copy that never
joined, and it still carried the exact drift the extraction removed:

    HTTPException(status_code=400, detail="")

    in-tree -> {"detail": "",      "status_code": 400}
    public  -> {"detail": "Error", "status_code": 400}

and two the extraction had since added and it had never had:

    body_too_large(100)

    in-tree -> {..., "status_code": 413, "limit": 100}
    public  -> {..., "status_code": 413}            # the limit is gone

An application that registers the exported handler - which the error-handling
guide tells it to - therefore got a different error contract from the default.
The builder now lives in `exceptions.py` (the layer that owns `HTTPException`,
and low enough for `app/` to import it) and all three call it.
"""

from __future__ import annotations

import json

import pytest

from veloce import Veloce
from veloce.exceptions import (
    HTTPException,
    NotFound,
    RequestValidationError,
    http_exception_handler,
    http_exception_payload,
)
from veloce.http._body import body_too_large
from veloce.testclient import TestClient

CASES = {
    "empty-detail": lambda: HTTPException(status_code=400, detail=""),
    "with-detail": lambda: HTTPException(status_code=404, detail="nope"),
    "subclass-description": lambda: NotFound(),
    "body-limit": lambda: body_too_large(100),
    "validation": lambda: RequestValidationError([{"loc": ["q"], "msg": "m", "type": "t"}]),
}


async def _public_body(exc) -> dict:
    response = await http_exception_handler(None, exc)
    return json.loads(response.body)


# ── the public handler agrees with the shared builder ────────────────


@pytest.mark.parametrize("name", list(CASES))
async def test_the_public_handler_renders_the_shared_payload(name):
    exc = CASES[name]()
    assert await _public_body(exc) == http_exception_payload(exc)


async def test_an_empty_detail_is_not_substituted():
    """The drift the extraction removed and this copy kept."""
    assert (await _public_body(HTTPException(status_code=400, detail="")))["detail"] == ""


async def test_a_body_limit_refusal_names_its_limit():
    """The field the public handler never carried."""
    assert (await _public_body(body_too_large(100)))["limit"] == 100


async def test_the_response_status_matches_the_payload():
    """They were computed separately, so they could disagree."""
    response = await http_exception_handler(None, HTTPException(status_code=404, detail="x"))
    assert response.status_code == json.loads(response.body)["status_code"]


# ── and with what a real request emits ───────────────────────────────


@pytest.mark.parametrize("name", ["empty-detail", "with-detail", "subclass-description"])
def test_the_public_handler_matches_the_request_cycle(name):
    """The comparison that matters: an application registering the exported
    handler must get the same contract as the default."""
    exc_factory = CASES[name]

    default_app = Veloce(openapi_url=None)

    @default_app.get("/boom")
    async def boom_default():
        raise exc_factory()

    registered_app = Veloce(openapi_url=None)
    registered_app.register_error_handler(HTTPException, http_exception_handler)

    @registered_app.get("/boom")
    async def boom_registered():
        raise exc_factory()

    default = TestClient(default_app).get("/boom")
    registered = TestClient(registered_app).get("/boom")
    assert default.json() == registered.json()
    assert default.status_code == registered.status_code


# ── the subclass description still reaches the body ──────────────────
#
# The negative. The public copy had its own `getattr(exc, "description", "")`
# fallback; dropping it would be a regression if `detail` did not already fall
# back to `description` at construction. It does - asserted, not assumed.


def test_a_subclass_description_becomes_the_detail():
    assert http_exception_payload(NotFound())["detail"] == NotFound.description
    assert NotFound.description != ""


async def test_the_public_handler_carries_the_subclass_description():
    assert (await _public_body(NotFound()))["detail"] == NotFound.description


async def test_headers_are_still_carried():
    """The handler's other job, which the consolidation must not drop."""
    exc = HTTPException(status_code=401, detail="no", headers={"WWW-Authenticate": "Bearer"})
    response = await http_exception_handler(None, exc)
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_a_validation_errors_list_is_emitted_verbatim():
    errors = [{"loc": ["q"], "msg": "m", "type": "t"}]
    assert (await _public_body(RequestValidationError(errors)))["detail"] == errors


def test_there_is_one_payload_builder():
    """`app.errors` re-exports rather than keeping a copy."""
    from veloce.app.errors import http_exception_payload as app_copy

    assert app_copy is http_exception_payload
