"""GZip reads both spellings of the Accept-Encoding weight parameter.

A client writing `q=0` and one writing `Q=0` expressed the same refusal, and
the middleware honoured only one of them.

Split out of `test_entry_point_parity.py` - see that module's history in
`test_config_env_loaders.py`.
"""

from __future__ import annotations

import pytest

from veloce import GZipMiddleware, Response, Veloce
from veloce.testclient import TestClient


def _gzip_app() -> TestClient:
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=100))

    @app.get("/x")
    async def x():

        return Response(body=b"x" * 5000, content_type="text/plain")

    return TestClient(app)


@pytest.mark.parametrize(
    "accept",
    ["gzip;q=0", "gzip;Q=0", "gzip; Q=0", "gzip;Q=0.0", "*;q=0", "*;Q=0", "identity;q=1, gzip;Q=0"],
)
def test_a_refusal_is_honoured_in_either_spelling(accept):
    """The defect: the upper-case spellings got a gzipped body anyway."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") != "gzip"


@pytest.mark.parametrize("accept", ["gzip", "gzip;q=1", "gzip;Q=1", "gzip;q=0.5", "*"])
def test_an_acceptance_still_compresses(accept):
    """The negative: refusing everything would pass the test above vacuously."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") == "gzip"


@pytest.mark.parametrize("accept", ["gzip;q=bad", "gzip;Q=bad"])
def test_an_unparseable_weight_is_ignored_in_either_spelling(accept):
    """A malformed weight is not a refusal; both spellings must agree on that."""
    response = _gzip_app().get("/x", headers={"Accept-Encoding": accept})
    assert response.headers.get("Content-Encoding") == "gzip"


@pytest.mark.parametrize("spelling", ["q", "Q"])
def test_the_middleware_agrees_with_the_shared_parser(spelling):
    """`request.accept_encodings` already read both; the middleware is the copy that did not."""
    header = f"gzip;{spelling}=0"
    seen: dict = {}

    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(request) -> dict:
        seen["quality"] = request.accept_encodings.quality("gzip")
        return {}

    TestClient(app).get("/q", headers={"Accept-Encoding": header})
    assert seen["quality"] == 0.0
    response = _gzip_app().get("/x", headers={"Accept-Encoding": header})
    assert response.headers.get("Content-Encoding") != "gzip"
