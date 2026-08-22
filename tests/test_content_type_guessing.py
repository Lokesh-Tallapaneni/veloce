"""Naming a file's media type, once, for both callers.

The static server memoized `mimetypes.guess_type` because it "walks the
registered MIME table on every call"; `FileResponse` called the same function
uncached on every response it built. Identical answers, one of them roughly
thirty times dearer - and had a user registered a type after start, the two
would have disagreed on the answer as well as the cost.
"""

from __future__ import annotations

import mimetypes

import pytest

from veloce import FileResponse, TestClient, Veloce
from veloce._internal import MIME_OCTET_STREAM, guess_content_type


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("styles.css", "text/css"),
        ("bundle.js", "text/javascript"),
        ("logo.png", "image/png"),
        ("data.json", "application/json"),
        ("/deep/nested/path/report.pdf", "application/pdf"),
    ],
)
def test_a_known_extension_is_named(path: str, expected: str):
    assert guess_content_type(path) == expected


@pytest.mark.parametrize("path", ["archive.unknownext", "noextension", "", ".", "trailing."])
def test_an_unknown_extension_falls_back_to_octet_stream(path: str):
    assert guess_content_type(path) == MIME_OCTET_STREAM


def test_it_agrees_with_the_standard_library():
    for path in ("a.css", "b.js", "c.png", "d.unknownext", "e"):
        assert guess_content_type(path) == (mimetypes.guess_type(path)[0] or MIME_OCTET_STREAM)


def test_the_answer_is_memoized():
    """Both callers hit the same handful of extensions over and over."""
    guess_content_type("memoized-probe.css")
    before = guess_content_type.cache_info().hits
    guess_content_type("memoized-probe.css")
    assert guess_content_type.cache_info().hits == before + 1


def test_the_cache_is_bounded():
    """An arbitrary path is a cache key, so a prober must not grow it forever."""
    assert guess_content_type.cache_info().maxsize == 512


def test_a_file_response_names_its_type_through_the_shared_helper(tmp_path):
    stylesheet = tmp_path / "site.css"
    stylesheet.write_text("body{}", encoding="utf-8")
    response = FileResponse(str(stylesheet))
    assert response.content_type.startswith("text/css")


def test_an_explicit_content_type_still_wins(tmp_path):
    stylesheet = tmp_path / "site.css"
    stylesheet.write_text("body{}", encoding="utf-8")
    response = FileResponse(str(stylesheet), content_type="text/plain")
    assert response.content_type.startswith("text/plain")


def test_a_served_file_and_a_static_file_are_named_alike(tmp_path):
    """One handler, one static mount, one answer for the same extension."""
    (tmp_path / "shared.css").write_text("body{}", encoding="utf-8")
    app = Veloce(title="Files", openapi_url=None)

    @app.get("/download")
    async def download() -> FileResponse:
        return FileResponse(str(tmp_path / "shared.css"))

    from veloce.contrib.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(tmp_path)))

    with TestClient(app) as client:
        served = client.get("/download").headers["content-type"]
        statically = client.get("/static/shared.css").headers["content-type"]
    assert served.split(";")[0] == statically.split(";")[0] == "text/css"
