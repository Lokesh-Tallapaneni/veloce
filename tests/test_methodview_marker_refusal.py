"""A `MethodView` verb method refuses a marker it cannot resolve.

`docs/guide/class-based-views.md` said "Declarative parameter markers and
dependency injection work too — annotate the method parameters as you would on a
function handler", and showed this example:

    class SearchView(MethodView):
        async def get(self, request: Request, q: str = Query(default="")):
            return {"query": q}

They do not work. A `MethodView` is **one** route serving several verbs, so the
route has one handler plan and nothing resolves a per-verb marker. The default
object was passed straight through, so the documented example answered with the
repr of a marker:

    GET /search?q=abc  ->  {"query": "<veloce._params.Query object at 0x...>"}
    GET /dep           ->  {"d":     "<veloce.dependency.Depends object at 0x...>"}

and the operation's OpenAPI `parameters` was `null`.

Implementing per-verb plans is a routing change, not a bug fix, so the behaviour
stands and the two halves that were wrong are fixed: the documentation now says
what works, and the mistake is refused at **class-definition time** — on import,
the same moment a sync verb method is refused — rather than reaching production
as a view that returns nonsense with a 200.
"""

from __future__ import annotations

import pytest

from veloce import Cookie, Depends, Header, MethodView, Path, Query, Request, Veloce
from veloce.testclient import TestClient


def _dep() -> str:
    return "from-dep"


# ── the mistake is refused on import ─────────────────────────────────


@pytest.mark.parametrize(
    "marker",
    [Query(default=""), Header(default=""), Cookie(default=""), Path(), Depends(_dep)],
    ids=["Query", "Header", "Cookie", "Path", "Depends"],
)
def test_a_verb_method_declaring_a_marker_is_refused(marker):
    """The defect: this was accepted and the marker object became the value."""
    with pytest.raises(TypeError, match="cannot resolve"):

        class Bad(MethodView):
            async def get(self, request: Request, value: str = marker) -> dict:
                return {}


def test_the_message_names_the_parameter():
    with pytest.raises(TypeError, match="declares q"):

        class Bad(MethodView):
            async def get(self, request: Request, q: str = Query(default="")) -> dict:
                return {}


def test_the_message_says_what_to_do_instead():
    with pytest.raises(TypeError, match="function handler"):

        class Bad(MethodView):
            async def get(self, request: Request, q: str = Query(default="")) -> dict:
                return {}


def test_several_offenders_are_all_named():
    with pytest.raises(TypeError, match="q, d"):

        class Bad(MethodView):
            async def get(
                self, request: Request, q: str = Query(default=""), d: str = Depends(_dep)
            ) -> dict:
                return {}


@pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete"])
def test_every_verb_is_checked(verb):
    async def handler(self, request: Request, q: str = Query(default="")) -> dict:
        return {}

    # Built with `type()` rather than `exec()` of a source string, so the verb
    # method is ordinary code that ruff, mypy and grep can all see.
    with pytest.raises(TypeError, match="cannot resolve"):
        type("Bad", (MethodView,), {verb: handler})


def test_the_refusal_happens_at_class_definition_not_at_request():
    """Like the `async def` check: the mistake surfaces on import."""
    raised = False
    try:

        class Bad(MethodView):
            async def get(self, request: Request, q: str = Query(default="")) -> dict:
                return {}
    except TypeError:
        raised = True
    # No app, no route, no request was needed to find it.
    assert raised


# ── everything a MethodView can actually do still works ──────────────


def test_a_plain_verb_method_works():
    app = Veloce(openapi_url=None)

    class View(MethodView):
        async def get(self, request: Request) -> dict:
            return {"ok": True}

    app.add_url_rule("/x", view_func=View.as_view("x"))
    assert TestClient(app).get("/x").json() == {"ok": True}


def test_path_parameters_still_arrive():
    app = Veloce(openapi_url=None)

    class View(MethodView):
        async def get(self, request: Request, item_id: int) -> dict:
            return {"id": item_id}

    app.add_url_rule("/i/{item_id:int}", view_func=View.as_view("i"))
    assert TestClient(app).get("/i/7").json() == {"id": 7}


def test_the_corrected_documentation_example_runs():
    """Reading the value off the request is the working form the guide shows."""
    app = Veloce(openapi_url=None)

    class SearchView(MethodView):
        async def get(self, request: Request) -> dict:
            return {"query": request.query_params.get("q", "")}

    app.add_url_rule("/search", view_func=SearchView.as_view("search"))
    client = TestClient(app)
    assert client.get("/search?q=abc").json() == {"query": "abc"}
    assert client.get("/search").json() == {"query": ""}


def test_an_ordinary_default_is_not_refused():
    """Only a marker is unresolvable; a plain default is fine."""
    app = Veloce(openapi_url=None)

    class View(MethodView):
        async def get(self, request: Request, item_id: int = 5) -> dict:
            return {"id": item_id}

    app.add_url_rule("/i", view_func=View.as_view("i"))
    assert TestClient(app).get("/i").json() == {"id": 5}


def test_several_verbs_still_dispatch():
    app = Veloce(openapi_url=None)

    class View(MethodView):
        async def get(self, request: Request) -> dict:
            return {"verb": "get"}

        async def post(self, request: Request) -> dict:
            return {"verb": "post"}

    app.add_url_rule("/x", view_func=View.as_view("x"))
    client = TestClient(app)
    assert client.get("/x").json() == {"verb": "get"}
    assert client.post("/x").json() == {"verb": "post"}


def test_a_sync_verb_method_is_still_refused():
    """The check this one sits beside must keep working."""
    with pytest.raises(TypeError, match="must be async"):

        class Bad(MethodView):
            def get(self, request: Request) -> dict:  # type: ignore[misc]
                return {}


def test_a_function_handler_still_resolves_markers():
    """The negative: markers work where there is a plan to resolve them."""
    app = Veloce(openapi_url=None)

    @app.get("/f")
    async def f(q: str = Query(default=""), d: str = Depends(_dep)) -> dict:
        return {"q": q, "d": d}

    assert TestClient(app).get("/f?q=abc").json() == {"q": "abc", "d": "from-dep"}
