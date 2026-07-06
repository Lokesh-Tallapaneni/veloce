"""RFC 9110 modern status-name aliases in veloce.status.

RFC 9110 renamed four 4xx reason phrases. The modern spellings are exposed
alongside the legacy ones, both pointing at the same integer code.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce, status


def test_content_too_large_aliases_413():
    assert status.HTTP_413_CONTENT_TOO_LARGE == 413
    assert status.HTTP_413_CONTENT_TOO_LARGE == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


def test_uri_too_long_aliases_414():
    assert status.HTTP_414_URI_TOO_LONG == 414
    assert status.HTTP_414_URI_TOO_LONG == status.HTTP_414_REQUEST_URI_TOO_LONG


def test_range_not_satisfiable_aliases_416():
    assert status.HTTP_416_RANGE_NOT_SATISFIABLE == 416
    assert status.HTTP_416_RANGE_NOT_SATISFIABLE == status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE


def test_unprocessable_content_aliases_422():
    assert status.HTTP_422_UNPROCESSABLE_CONTENT == 422
    assert status.HTTP_422_UNPROCESSABLE_CONTENT == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_legacy_names_retained():
    # Back-compat: the original spellings must keep working.
    assert status.HTTP_413_REQUEST_ENTITY_TOO_LARGE == 413
    assert status.HTTP_414_REQUEST_URI_TOO_LONG == 414
    assert status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE == 416
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422


def test_modern_names_are_plain_ints():
    # Plain module constants, not a PEP 562 lookup - directly usable as ints.
    assert isinstance(status.HTTP_413_CONTENT_TOO_LARGE, int)
    assert status.HTTP_422_UNPROCESSABLE_CONTENT + 0 == 422


class TestStatusModule:
    def test_common_status_codes(self):
        assert status.HTTP_200_OK == 200
        assert status.HTTP_201_CREATED == 201
        assert status.HTTP_204_NO_CONTENT == 204
        assert status.HTTP_301_MOVED_PERMANENTLY == 301
        assert status.HTTP_400_BAD_REQUEST == 400
        assert status.HTTP_401_UNAUTHORIZED == 401
        assert status.HTTP_403_FORBIDDEN == 403
        assert status.HTTP_404_NOT_FOUND == 404
        assert status.HTTP_405_METHOD_NOT_ALLOWED == 405
        assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422
        assert status.HTTP_429_TOO_MANY_REQUESTS == 429
        assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500

    @pytest.mark.asyncio
    async def test_status_code_in_route(self):
        app = Veloce(openapi_url=None)

        @app.post("/items", status_code=status.HTTP_201_CREATED)
        async def create(request: Request):
            return {"id": 1}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201
