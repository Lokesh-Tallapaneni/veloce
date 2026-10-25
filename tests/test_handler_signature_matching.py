"""An exception handler is bound by name, then annotation, then position.

Veloce matched an exception handler's parameters by name alone, so a handler
whose parameters were not literally called `request` and `exc` was called with
nothing at all:

    @app.exception_handler(ValueError)
    async def handle(req, error):       # TypeError: missing 2 arguments -> 500
        return JSONResponse({"detail": str(error)}, status_code=400)

Passing the request and the exception positionally is the prevailing
convention, so a handler written elsewhere declares whatever names its author
liked - and pasting one in answered 500 rather than running. Name matching
still wins where it applies; an annotation decides next, and position last,
which is the rule those handlers were written for. A lone unmatched parameter
is the one exception to that order - `(response)` and `(exc)` are what a
one-argument author means, so it receives the second value, not the request.

The same adapter serves after-request hooks, so both configurations are
exercised here: the hook's second value is the response, the handler's is the
exception.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Request, Response, Veloce
from veloce.app import dispatch
from veloce.app.dispatch import _adapt_hook_kwargs, _resolve_hook_binding
from veloce.testclient import TestClient


def _exc_app(handler):
    app = Veloce(openapi_url=None)
    app.exception_handler(ValueError)(handler)

    @app.get("/boom")
    async def boom():
        raise ValueError("the real message")

    return TestClient(app)


def _after_app(hook):
    app = Veloce(openapi_url=None)
    app.after_request(hook)

    @app.get("/")
    async def index():
        return {"ok": True}

    return TestClient(app)


# ── the reported defect ──────────────────────────────────────────────


def test_an_exception_handler_with_its_own_parameter_names_is_called():
    """The defect verbatim: this answered 500 with both arguments missing."""

    async def handle(req, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    resp = _exc_app(handle).get("/boom")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "the real message"}


def test_an_exception_handler_with_its_own_names_receives_the_request_too():
    """Both values arrive, not just the one the body happened to read."""
    seen = {}

    async def handle(req, error):
        seen["path"] = req.path
        seen["message"] = str(error)
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert seen == {"path": "/boom", "message": "the real message"}


# ── binding by annotation ────────────────────────────────────────────


def test_annotations_bind_an_exception_handler_whose_names_do_not_match():
    seen = {}

    async def handle(r: Request, e: ValueError):
        seen["path"] = r.path
        seen["message"] = str(e)
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert seen == {"path": "/boom", "message": "the real message"}


def test_annotations_bind_an_exception_handler_declared_in_the_other_order():
    """Position alone would get this backwards; the annotation decides first."""

    async def handle(e: Exception, r: Request):
        return JSONResponse({"detail": str(e), "path": r.path}, status_code=400)

    assert _exc_app(handle).get("/boom").json() == {
        "detail": "the real message",
        "path": "/boom",
    }


def test_annotations_bind_an_after_request_hook_declared_in_the_other_order():
    seen = {}

    async def hook(out: Response, incoming: Request):
        seen["path"] = incoming.path
        seen["status"] = out.status_code
        return out

    assert _after_app(hook).get("/").status_code == 200
    assert seen == {"path": "/", "status": 200}


def test_an_unresolvable_annotation_falls_through_to_position():
    """A broken annotation must not raise; the positional rule still binds."""

    async def handle(req: NoSuchType, error):  # noqa: F821
        return JSONResponse({"detail": str(error)}, status_code=400)

    assert _exc_app(handle).get("/boom").json() == {"detail": "the real message"}


def test_an_unresolvable_annotation_on_the_second_parameter_falls_through():
    async def handle(req, error: AlsoMissing):  # noqa: F821
        return JSONResponse({"detail": str(error)}, status_code=400)

    assert _exc_app(handle).get("/boom").json() == {"detail": "the real message"}


# ── one parameter, which means the second value ──────────────────────
#
# A lone unmatched parameter used to answer 500 ("missing 1 required positional
# argument"), and binding the request to it instead replaced that with silence:
# the hook ran, did its work against the wrong object, and an after-request
# hook's return value is kept only when it is a `Response`, so nothing landed
# and nothing said so. `(response)` and `(exc)` are what a one-argument author
# of these two callbacks means.


def test_a_single_parameter_exception_handler_receives_the_exception():
    """`(e)` is the one-argument spelling, so it must see the error - binding
    the request here answered 418 with the body `Request`."""

    async def handle(e):
        return Response(body=str(e).encode(), status_code=418)

    resp = _exc_app(handle).get("/boom")
    assert resp.status_code == 418
    assert resp.body == b"the real message"


def test_a_single_parameter_after_request_hook_receives_the_response():
    """The silent shape: binding the request left the header unset, and a hook
    returning anything but a `Response` is discarded without a word."""

    async def hook(resp):
        resp.headers["X-Hook"] = "ran"
        return resp

    response = _after_app(hook).get("/")
    assert response.status_code == 200
    assert response.headers["X-Hook"] == "ran"


def test_a_single_parameter_annotated_request_still_receives_the_request():
    """The escape hatch: the annotation is read before position."""
    seen = {}

    async def handle(whatever: Request):
        seen["path"] = whatever.path
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert seen["path"] == "/boom"


# ── the shapes that already worked ───────────────────────────────────


def test_a_var_keyword_handler_still_receives_both_under_the_canonical_names():
    seen = {}

    async def handle(**kwargs):
        seen.update(kwargs)
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert sorted(seen) == ["exc", "request"]
    assert str(seen["exc"]) == "the real message"


def test_a_zero_parameter_handler_still_runs():
    async def handle():
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418


def test_the_closure_capture_idiom_on_an_after_request_hook_keeps_its_default():
    """`_name=name` is how a hook carries its own third value; position must
    not overwrite it with the request."""
    seen = {}

    async def hook(response, _name="captured"):
        seen["name"] = _name
        return response

    assert _after_app(hook).get("/").status_code == 200
    assert seen["name"] == "captured"


def test_a_parameter_with_a_default_beyond_the_two_values_keeps_its_default():
    seen = {}

    async def handle(req, error, extra="kept"):
        seen["extra"] = extra
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert seen["extra"] == "kept"


# ── regression: the canonical names are unchanged ────────────────────


def test_the_canonically_named_exception_handler_is_unchanged():
    seen = {}

    async def handle(request, exc):
        seen["path"] = request.path
        seen["message"] = str(exc)
        return Response(body=b"ok", status_code=418)

    assert _exc_app(handle).get("/boom").status_code == 418
    assert seen == {"path": "/boom", "message": "the real message"}


def test_the_canonically_named_after_request_hook_is_unchanged():
    seen = {}

    async def hook(request, response):
        seen["path"] = request.path
        response.headers["X-Hook"] = "ran"
        return response

    assert _after_app(hook).get("/").headers["X-Hook"] == "ran"
    assert seen["path"] == "/"


def test_an_oddly_named_after_request_hook_now_runs():
    seen = {}

    async def hook(incoming, out):
        seen["path"] = incoming.path
        out.headers["X-Hook"] = "ran"
        return out

    assert _after_app(hook).get("/").headers["X-Hook"] == "ran"
    assert seen["path"] == "/"


def test_an_after_request_hook_naming_only_the_response_is_unchanged():
    """`response` by name, with no request - the shape the name rule protects."""

    async def hook(response):
        response.headers["X-Hook"] = "ran"
        return response

    assert _after_app(hook).get("/").headers["X-Hook"] == "ran"


@pytest.mark.parametrize(
    ("signature", "second_name", "expected"),
    [
        pytest.param("request, exc", "exc", ("request", "exc"), id="canonical-exc"),
        pytest.param(
            "request, response", "response", ("request", "response"), id="canonical-after"
        ),
        pytest.param("request", "exc", ("request", None), id="request-only"),
        pytest.param("exc", "exc", (None, "exc"), id="exc-only"),
        pytest.param("response", "response", (None, "response"), id="response-only"),
        pytest.param("", "exc", (None, None), id="nothing"),
        pytest.param("req, error", "exc", ("req", "error"), id="positional"),
        pytest.param("only", "exc", (None, "only"), id="one-parameter-is-the-second-value"),
        pytest.param("only", "response", (None, "only"), id="one-parameter-after-request"),
        pytest.param("only: Request", "exc", ("only", None), id="one-parameter-annotated-request"),
        pytest.param("request, error", "exc", ("request", "error"), id="name-then-position"),
        pytest.param("err, request", "exc", ("request", "err"), id="position-fills-around-a-name"),
        pytest.param("**kw", "exc", ("request", "exc"), id="var-keyword"),
        pytest.param("*args", "exc", (None, None), id="var-positional"),
        pytest.param("*, a, b", "exc", ("a", "b"), id="keyword-only"),
        pytest.param(
            "response, _name=1", "response", (None, "response"), id="closure-capture-kept"
        ),
        pytest.param("req, error, extra=1", "exc", ("req", "error"), id="default-beyond-the-two"),
    ],
)
def test_the_resolved_binding(signature, second_name, expected):
    """The binding table, asserted at the resolver rather than through a request."""
    namespace: dict = {"Request": Request, "Response": Response}
    exec(f"def fn({signature}): pass", namespace)
    assert _resolve_hook_binding(namespace["fn"], second_name) == expected


# ── a third required parameter ───────────────────────────────────────
#
# Only two values exist to pass, so a third required parameter cannot be
# filled. Registration is not where this is caught - the adapter is reached
# from the response path, not from `app.exception_handler` - so it is a runtime
# `TypeError` naming the parameters, raised in place of the one Python would
# raise with less said about why.


def test_a_third_required_parameter_raises_a_naming_type_error():
    def handle(req, error, extra):
        return None

    with pytest.raises(TypeError) as excinfo:
        _resolve_hook_binding(handle, "exc")
    message = str(excinfo.value)
    assert "extra" in message
    assert "only the request and the exc are passed" in message


def test_a_third_required_parameter_on_a_hook_names_the_response():
    def hook(request, response, extra):
        return None

    with pytest.raises(TypeError, match="only the request and the response are passed"):
        _resolve_hook_binding(hook, "response")


def test_a_third_required_parameter_degrades_to_a_500_not_a_crash():
    """`_dispatch_exc_handler` logs the handler's own failure and answers 500."""

    async def handle(req, error, extra):
        return Response(body=b"never", status_code=418)

    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.exception_handler(ValueError)(handle)

    @app.get("/boom")
    async def boom():
        raise ValueError("the real message")

    assert TestClient(app).get("/boom").status_code == 500


# ── the cache ────────────────────────────────────────────────────────


def test_the_signature_is_inspected_once_across_many_requests(monkeypatch):
    """The per-request cost is a cache lookup, not a signature walk."""
    calls = 0

    def counting_resolve(fn, second_name):
        nonlocal calls
        calls += 1
        return _resolve_hook_binding(fn, second_name)

    monkeypatch.setattr(dispatch, "_resolve_hook_binding", counting_resolve)

    async def handle(req, error):
        return Response(body=b"ok", status_code=418)

    client = _exc_app(handle)
    for _ in range(5):
        assert client.get("/boom").status_code == 418
    assert calls == 1


def test_the_resolved_binding_is_what_lands_in_the_cache():
    async def handle(req, error):
        return None

    cache: dict = {}
    kwargs = _adapt_hook_kwargs(handle, cache, "exc", object(), ValueError("x"))
    assert cache[handle] == ("req", "error")
    assert sorted(kwargs) == ["error", "req"]
