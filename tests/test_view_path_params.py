"""A class-based view receives its path parameters, and serves its own verbs.

Two defects in the usage `views.py` advertises in its own module docstring.

`as_view` generated `async def view(request, **path_params)`, but the handler
plan skips a VAR_KEYWORD parameter - correctly, since `**kwargs` is not an
injectable query parameter - so dispatch called the view with the request
alone. A verb method declaring a path parameter then raised `TypeError` and
surfaced as a 500. The view now reads the values from `request.path_params`,
where dispatch has already put them, which also keeps the plan and the hot
dispatch path untouched.

Separately, `as_view` sets `view.methods` from the verbs the class defines and
`add_url_rule` ignored it, defaulting to GET. A `MethodView` with `get` and
`post` was registered for GET alone and answered its own POST with 405.
"""

from __future__ import annotations

from veloce import MethodView, Veloce, View
from veloce.testclient import TestClient


class _UserView(MethodView):
    async def get(self, request, uid: int):  # noqa: ANN001, ANN201
        return {"verb": "get", "uid": uid, "type": type(uid).__name__}

    async def post(self, request, uid: int):  # noqa: ANN001, ANN201
        return {"verb": "post", "uid": uid}


class _SharedView(MethodView):
    """One instance reused across requests."""

    init_every_request = False

    async def get(self, request, code: str):  # noqa: ANN001, ANN201
        return {"code": code}


class _PlainView(View):
    async def dispatch_request(self, request):  # noqa: ANN001, ANN201
        return {"plain": True}


def _client(rule: str, view, **kwargs) -> TestClient:  # noqa: ANN001
    app = Veloce(openapi_url=None)
    app.add_url_rule(rule, view_func=view, **kwargs)
    return TestClient(app)


# ── Path parameters reach the verb method ────────────────────────────


def test_a_path_parameter_reaches_a_method_view():
    """The original defect: this raised TypeError and returned 500."""
    with _client("/users/{uid:int}", _UserView.as_view("user")) as client:
        body = client.get("/users/7").json()
    assert body == {"verb": "get", "uid": 7, "type": "int"}


def test_the_converter_still_applies():
    """A typed parameter must arrive converted, not as the raw string."""
    with _client("/users/{uid:int}", _UserView.as_view("user")) as client:
        assert client.get("/users/7").json()["type"] == "int"


def test_a_non_matching_converter_is_still_a_404():
    with _client("/users/{uid:int}", _UserView.as_view("user")) as client:
        assert client.get("/users/abc").status_code == 404


def test_a_shared_instance_view_also_receives_them():
    """`init_every_request = False` takes the other branch of `as_view`."""
    with _client("/codes/{code}", _SharedView.as_view("code")) as client:
        assert client.get("/codes/abc").json() == {"code": "abc"}


def test_a_view_with_no_path_parameters_is_unaffected():
    with _client("/plain", _PlainView.as_view("plain")) as client:
        assert client.get("/plain").json() == {"plain": True}


def test_several_path_parameters_all_arrive():
    class _Pair(MethodView):
        async def get(self, request, left: int, right: str):  # noqa: ANN001, ANN201
            return {"left": left, "right": right}

    with _client("/pair/{left:int}/{right}", _Pair.as_view("pair")) as client:
        assert client.get("/pair/3/abc").json() == {"left": 3, "right": "abc"}


# ── The view's own verbs are served ──────────────────────────────────


def test_every_verb_the_class_defines_is_registered():
    """`as_view` computes `view.methods`; registration must honour it."""
    with _client("/users/{uid:int}", _UserView.as_view("user")) as client:
        assert client.get("/users/1").json()["verb"] == "get"
        assert client.post("/users/2").json()["verb"] == "post"


def test_a_verb_the_class_does_not_define_is_refused():
    with _client("/users/{uid:int}", _UserView.as_view("user")) as client:
        assert client.delete("/users/1").status_code == 405


def test_an_explicit_methods_argument_still_wins():
    """Deriving the verbs must not take the choice away from the caller."""
    with _client("/users/{uid:int}", _UserView.as_view("user"), methods=["GET"]) as client:
        assert client.get("/users/1").status_code == 200
        assert client.post("/users/1").status_code == 405


def test_a_plain_function_still_defaults_to_get():
    """The derivation reads an attribute a function does not have."""
    app = Veloce(openapi_url=None)

    async def handler(request):  # noqa: ANN001, ANN202
        return {"ok": True}

    app.add_url_rule("/fn", view_func=handler)
    with TestClient(app) as client:
        assert client.get("/fn").status_code == 200
        assert client.post("/fn").status_code == 405


# ── A view that reads the values itself keeps working ────────────────


def test_a_verb_method_that_does_not_declare_them_still_works():
    """Reading `request.path_params` was the only shape that worked before.

    Forwarding the values unconditionally would have turned that shape into
    the `TypeError` the fix set out to remove, so the view forwards only what
    the target declares.
    """

    class _Manual(MethodView):
        async def get(self, request):  # noqa: ANN001, ANN201
            return {"uid": request.path_params["uid"]}

    with _client("/users/{uid:int}", _Manual.as_view("manual")) as client:
        assert client.get("/users/9").json() == {"uid": 9}


def test_a_verb_method_may_declare_only_some_of_them():
    class _Partial(MethodView):
        async def get(self, request, left: int):  # noqa: ANN001, ANN201
            return {"left": left, "right": request.path_params["right"]}

    with _client("/pair/{left:int}/{right}", _Partial.as_view("partial")) as client:
        assert client.get("/pair/4/xy").json() == {"left": 4, "right": "xy"}


def test_one_verb_may_declare_them_and_another_not():
    """The filter is per verb, so two methods on one class can differ."""

    class _Mixed(MethodView):
        async def get(self, request, uid: int):  # noqa: ANN001, ANN201
            return {"verb": "get", "uid": uid}

        async def post(self, request):  # noqa: ANN001, ANN201
            return {"verb": "post", "uid": request.path_params["uid"]}

    with _client("/users/{uid:int}", _Mixed.as_view("mixed")) as client:
        assert client.get("/users/1").json() == {"verb": "get", "uid": 1}
        assert client.post("/users/2").json() == {"verb": "post", "uid": 2}


def test_a_verb_taking_kwargs_receives_every_value():
    class _Kwargs(MethodView):
        async def get(self, request, **params):  # noqa: ANN001, ANN201
            return params

    with _client("/pair/{left:int}/{right}", _Kwargs.as_view("kw")) as client:
        assert client.get("/pair/5/z").json() == {"left": 5, "right": "z"}


def test_a_plain_view_reading_them_itself_works():
    """`View.dispatch_request` declares no path parameters at all."""

    class _Reader(View):
        methods = ["GET"]

        async def dispatch_request(self, request):  # noqa: ANN001, ANN201
            return {"code": request.path_params["code"]}

    with _client("/codes/{code}", _Reader.as_view("reader")) as client:
        assert client.get("/codes/abc").json() == {"code": "abc"}


def test_an_inherited_verb_is_filtered_by_its_own_signature():
    """Each subclass rebuilds the map, so the MRO decides which method wins."""

    class _Base(MethodView):
        async def get(self, request):  # noqa: ANN001, ANN201
            return {"from": "base", "uid": request.path_params["uid"]}

    class _Child(_Base):
        pass

    with _client("/users/{uid:int}", _Child.as_view("child")) as client:
        assert client.get("/users/3").json() == {"from": "base", "uid": 3}
