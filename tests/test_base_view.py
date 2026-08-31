"""View base class single-dispatch class-based view."""

from __future__ import annotations

import functools

import pytest

from tests.conftest import make_request
from veloce import MethodView, Request, Veloce, View
from veloce.testclient import TestClient


def _req(method: str = "GET") -> Request:
    return make_request(method=method)


def test_view_dispatch_request_routed():
    app = Veloce()

    class Index(View):
        async def dispatch_request(self, request):
            return {"page": "index"}

    app.add_url_rule("/", view_func=Index.as_view("index"))
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"page": "index"}


def test_view_base_dispatch_not_implemented_raises():
    """A view that omits `dispatch_request` is refused where it is written.

    This used to construct and raise `NotImplementedError` on the first request
    to the route it was registered for. `View.__init_subclass__` now catches it
    at class definition, so the omission surfaces on import instead.
    """
    with pytest.raises(TypeError, match="dispatch_request"):

        class Bare(View):
            pass


async def test_view_base_dispatch_still_raises_when_called_directly():
    """The base's own method keeps its `NotImplementedError` body - the guard
    sits in front of it rather than replacing it."""

    with pytest.raises(NotImplementedError):
        await View().dispatch_request(_req())


def test_as_view_sets_name_and_view_class():
    class Index(View):
        async def dispatch_request(self, request):
            return {}

    view = Index.as_view("home")
    assert view.__name__ == "home"
    assert view.view_class is Index


def test_init_every_request_true_makes_fresh_instances():
    # Keep a reference to every instance for the whole test so their
    # lifetimes overlap — `is`-distinctness is then sound. Comparing
    # id() across non-overlapping lifetimes is not: CPython reuses the
    # address of a collected object.
    instances: list[View] = []

    class Counter(View):
        def __init__(self):
            instances.append(self)

        async def dispatch_request(self, request):
            return {}

    app = Veloce()
    app.add_url_rule("/c", view_func=Counter.as_view("c"))
    with TestClient(app) as client:
        client.get("/c")
        client.get("/c")
    # Two requests → two instances, both still referenced here.
    assert len(instances) == 2
    assert instances[0] is not instances[1]


def test_init_every_request_false_reuses_instance():
    instances: list[int] = []

    class Shared(View):
        init_every_request = False

        def __init__(self):
            instances.append(id(self))

        async def dispatch_request(self, request):
            return {}

    app = Veloce()
    app.add_url_rule("/s", view_func=Shared.as_view("s"))
    with TestClient(app) as client:
        client.get("/s")
        client.get("/s")
    # Instance built once at as_view() time, reused for both requests.
    assert len(instances) == 1


def test_decorators_are_applied():
    calls: list[str] = []

    def log_decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            calls.append("decorated")
            return await fn(*args, **kwargs)

        return wrapper

    class Decorated(View):
        decorators = [log_decorator]

        async def dispatch_request(self, request):
            return {"ok": True}

    app = Veloce()
    app.add_url_rule("/d", view_func=Decorated.as_view("d"))
    with TestClient(app) as client:
        resp = client.get("/d")
        assert resp.json() == {"ok": True}
    assert calls == ["decorated"]


def test_methodview_still_subclasses_view():

    assert issubclass(MethodView, View)
