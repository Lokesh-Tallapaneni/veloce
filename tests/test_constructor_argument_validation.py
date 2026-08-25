"""Two constructor arguments that were accepted, stored, and quietly wrong.

**`exception_handlers=` with a string key.** The mapping branches on
`isinstance(key, int)`; everything else went into `_exception_handlers`, which is
matched by walking a raised exception's MRO. A string can never appear in an
MRO, so `{"404": h, "ValueError": h}` registered cleanly and no handler ever
fired:

    404 raised     -> {"detail": "Not Found", "status_code": 404}   (default)
    ValueError     -> {"detail": "Internal Server Error"}           (default)
    stored keys    -> ['404', 'ValueError']

String keys are realistic — the mapping often comes from JSON, TOML or the
environment, none of which can hold an `int` key or a Python class.

**`docs_url=""`.** `openapi_url` is guarded on truthiness, `docs_url` was
compared to `None`, so an empty string registered Swagger UI at the site root:

    GET /  -> 200 text/html, the Swagger page

And if the app also owned `/`, the collision surfaced on the *first request*
rather than at construction, because the documentation routes register lazily.
The same lazy failure occurred for `docs_url == redoc_url`, where the error
escaped on a request to any route at all, including an unrelated one.
"""

from __future__ import annotations

import pytest

from veloce import HTTPException, Veloce
from veloce.testclient import TestClient


def handler(request, exc):
    return {"handled": True}


# ── exception_handlers keys ──────────────────────────────────────────


@pytest.mark.parametrize("key", ["404", "500", "ValueError", "HTTPException", "", "not a code"])
def test_a_string_key_is_refused(key):
    """The defect: stored in a table matched by MRO walk, so never found."""
    with pytest.raises(TypeError, match="error handler keys"):
        Veloce(openapi_url=None, exception_handlers={key: handler})


@pytest.mark.parametrize("key", [3.5, None, object(), ("tuple",), b"404"])
def test_a_non_class_non_int_key_is_refused(key):
    with pytest.raises(TypeError, match="error handler keys"):
        Veloce(openapi_url=None, exception_handlers={key: handler})


def test_a_numeric_string_is_told_to_drop_the_quotes():
    with pytest.raises(TypeError, match="Write 404 without the quotes"):
        Veloce(openapi_url=None, exception_handlers={"404": handler})


def test_a_class_name_string_is_told_to_pass_the_class():
    with pytest.raises(TypeError, match="Pass the class itself, not its name"):
        Veloce(openapi_url=None, exception_handlers={"ValueError": handler})


def test_a_non_exception_class_is_refused():
    """A class that cannot be raised can never match an MRO walk either."""
    with pytest.raises(TypeError, match="error handler keys"):
        Veloce(openapi_url=None, exception_handlers={dict: handler})


def test_register_error_handler_refuses_the_same_keys():
    """The constructor routes through it, and so does user code."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="error handler keys"):
        app.register_error_handler("404", handler)


# ── the valid keys still work ────────────────────────────────────────


def test_an_int_status_key_fires():
    app = Veloce(openapi_url=None, exception_handlers={404: handler})

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/missing").json() == {"handled": True}


def test_an_exception_class_key_fires():
    def on_value_error(request, exc):
        return {"caught": str(exc)}

    app = Veloce(openapi_url=None, exception_handlers={ValueError: on_value_error})

    @app.get("/x")
    async def x() -> dict:
        raise ValueError("boom")

    assert TestClient(app).get("/x").json() == {"caught": "boom"}


def test_a_base_class_key_catches_a_subclass():
    """The MRO walk is the reason the table holds classes."""

    def on_http(request, exc):
        return {"caught": exc.status_code}

    app = Veloce(openapi_url=None, exception_handlers={HTTPException: on_http})

    @app.get("/x")
    async def x() -> dict:
        raise HTTPException(status_code=418, detail="teapot")

    assert TestClient(app).get("/x").json() == {"caught": 418}


def test_both_key_kinds_together():
    app = Veloce(openapi_url=None, exception_handlers={404: handler, ValueError: handler})
    assert list(app._status_handlers) == [404]
    assert [c.__name__ for c in app._exception_handlers] == ["ValueError"]


def test_an_empty_mapping_is_fine():
    app = Veloce(openapi_url=None, exception_handlers={})

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").json() == {"ok": True}


def test_no_mapping_is_fine():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").json() == {"ok": True}


def test_the_decorator_form_is_unaffected():
    app = Veloce(openapi_url=None)

    @app.exception_handler(ValueError)
    def on_value_error(request, exc):
        return {"caught": True}

    @app.get("/x")
    async def x() -> dict:
        raise ValueError("boom")

    assert TestClient(app).get("/x").json() == {"caught": True}


# ── docs_url / redoc_url ─────────────────────────────────────────────


def test_an_empty_docs_url_disables_swagger():
    """The defect: it mounted the page at `/`."""
    app = Veloce(docs_url="")

    @app.get("/")
    async def home() -> dict:
        return {"home": True}

    assert TestClient(app).get("/").json() == {"home": True}


def test_an_empty_redoc_url_disables_redoc():
    app = Veloce(redoc_url="")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs").status_code == 200


def test_an_empty_docs_url_leaves_the_schema_and_redoc():
    app = Veloce(docs_url="")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/docs").status_code == 404


def test_an_empty_string_matches_none():
    """Two spellings of "disabled" must not behave differently."""
    for value in ("", None):
        app = Veloce(docs_url=value)

        @app.get("/x", name="x")
        async def x() -> dict:
            return {}

        assert TestClient(app).get("/docs").status_code == 404


def test_both_empty_disables_both_uis():
    app = Veloce(docs_url="", redoc_url="")

    @app.get("/")
    async def home() -> dict:
        return {"home": True}

    client = TestClient(app)
    assert client.get("/").json() == {"home": True}
    assert client.get("/openapi.json").status_code == 200


# ── a shared documentation path ──────────────────────────────────────


def test_two_uis_on_one_path_are_refused_at_construction():
    """It used to escape on a request to any route, including an unrelated one."""
    with pytest.raises(ValueError, match="cannot share a path"):
        Veloce(docs_url="/ui", redoc_url="/ui")


def test_the_refusal_names_the_path():
    with pytest.raises(ValueError, match="/ui"):
        Veloce(docs_url="/ui", redoc_url="/ui")


def test_the_refusal_says_what_to_do():
    with pytest.raises(ValueError, match="pass None to disable one"):
        Veloce(docs_url="/ui", redoc_url="/ui")


def test_both_disabled_is_not_a_collision():
    """`None == None` must not read as a shared path."""
    app = Veloce(docs_url=None, redoc_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/openapi.json").status_code == 200


def test_both_empty_is_not_a_collision():
    app = Veloce(docs_url="", redoc_url="")

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/openapi.json").status_code == 200


def test_one_empty_and_one_set_is_not_a_collision():
    app = Veloce(docs_url="", redoc_url="/reference")

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/reference").status_code == 200


def test_distinct_custom_paths_are_fine():
    app = Veloce(docs_url="/swagger", redoc_url="/reference")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/swagger").status_code == 200
    assert client.get("/reference").status_code == 200


# ── the default app is unchanged ─────────────────────────────────────


def test_the_default_urls_still_serve():
    app = Veloce()

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_openapi_url_none_still_disables_everything():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
