"""A re-registered route keeps every field the original declared.

A route reaches the app's tree three ways: directly, spliced from a blueprint,
and merged from an included router. The blueprint path and the merge path each
rebuild the `RouteInfo`, and each had drifted from `add_route` and from the
other - `_readd_route` dropped five fields and `_build_merged_route_info`
dropped `stream`, so `stream=True` on a blueprint route silently did nothing
and the route was served buffered.

The parity test below is the guard: a new `RouteInfo` field that either copy
forgets fails here rather than being discovered as a silent behaviour change.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import typing

import pytest

from veloce import Blueprint, Router, Veloce
from veloce.routing.router import RouteInfo

_ROUTER_SRC = pathlib.Path(inspect.getfile(Router)).read_text(encoding="utf-8")

#: Fields that legitimately differ on a re-registered route. The first group is
#: rewritten by the merge itself - the prefix changes the path and name, and the
#: parent's tags, dependencies and responses are combined in. The second is
#: derived from the handler at registration time (`_finalize_plans`), so carrying a
#: stale copy across would be wrong, not right.
_REWRITTEN = {
    "name",
    "path_template",
    "param_names",
    "handler",
    "dependencies",
    "tags",
    "responses",
    "response_class",
    # Recomputed per registration - see `router.py:969-981`.
    "handler_plan",
    "route_dep_plans",
    "is_trivial_plan",
    "is_request_only_plan",
    "is_fast_eligible",
    # Derived in `__init__` from the six `response_model_*` flags, which are
    # themselves forwarded, so each copy rebuilds it rather than carrying one.
    # Backed by behaviour tests over both copy paths in
    # `test_response_model_dump_kwargs.py`, not by this exemption alone.
    "response_dump_kwargs",
}


def _forwarded_in(function_name: str) -> set[str]:
    """Names of `RouteInfo` fields the named function reads off the source route.

    Matches every `info.<field>` in the body rather than an assignment shape:
    a field may be wrapped (`list(info.mcp_scopes)`), renamed on the way through
    (`exclude_middleware=info.excluded_middleware`), or assigned after
    construction, and all three of those are still carrying it.
    """
    start = _ROUTER_SRC.index(f"def {function_name}(")
    body = _ROUTER_SRC[start : start + 4000]
    return set(re.findall(r"\binfo\.([a-z_]+)", body))


@pytest.mark.parametrize("function_name", ["_readd_route", "_build_merged_route_info"])
def test_every_route_field_is_carried_across(function_name: str):
    """The guard: a field neither copy forwards is a silent behaviour change."""
    fields = {name for name in RouteInfo.__slots__ if not name.startswith("_")}
    missing = sorted(fields - _REWRITTEN - _forwarded_in(function_name))
    assert not missing, f"{function_name} does not carry: {missing}"


# ── The functional half ──────────────────────────────────────────────


def _streaming_blueprint() -> Blueprint:
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.post("/upload", stream=True)
    async def upload(request):
        return {"ok": True}

    return bp


def test_a_blueprint_route_keeps_stream():
    """The defect: this was served buffered, and nothing said so."""
    app = Veloce(openapi_url=None)
    app.register_blueprint(_streaming_blueprint())
    assert app.match("POST", "/bp/upload").route_info.stream is True


def test_a_nested_blueprint_route_keeps_stream():
    parent, child = Blueprint("p", url_prefix="/p"), Blueprint("c", url_prefix="/c")

    @child.post("/n", stream=True)
    async def nested(request):
        return {"ok": True}

    parent.register_blueprint(child)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)
    assert app.match("POST", "/p/c/n").route_info.stream is True


def test_an_included_router_route_keeps_stream():
    router = Router(prefix="/api")

    @router.post("/s", stream=True)
    async def streamed(request):
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.include_router(router)
    assert app.match("POST", "/api/s").route_info.stream is True


def test_a_non_streaming_route_is_unaffected():
    """The flag must not leak onto routes that did not ask for it."""
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.post("/plain")
    async def plain(request):
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    assert app.match("POST", "/bp/plain").route_info.stream is False


# ── The MCP resource fields the blueprint path dropped ───────────────


def _resource_blueprint() -> Blueprint:
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://doc",
        mcp_description="A document",
        mcp_resource_mime_type="text/markdown",
        mcp_resource_size=1234,
        mcp_resource_annotations={"audience": ["user"]},
        mcp_meta={"house": "value"},
    )
    async def doc():
        return {"body": "text"}

    return bp


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("mcp_resource_mime_type", "text/markdown"),
        ("mcp_resource_size", 1234),
        ("mcp_resource_annotations", {"audience": ["user"]}),
        ("mcp_meta", {"house": "value"}),
    ],
)
def test_a_blueprint_route_keeps_its_mcp_resource_metadata(field: str, expected):
    app = Veloce(openapi_url=None)
    app.register_blueprint(_resource_blueprint())
    assert getattr(app.match("GET", "/bp/doc").route_info, field) == expected


def test_the_same_metadata_survives_an_included_router():
    router = Router(prefix="/api")

    @router.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://api-doc",
        mcp_description="A document",
        mcp_resource_mime_type="application/json",
        mcp_resource_size=99,
    )
    async def doc():
        return {}

    app = Veloce(openapi_url=None)
    app.include_router(router)
    info = app.match("GET", "/api/doc").route_info
    assert info.mcp_resource_mime_type == "application/json"
    assert info.mcp_resource_size == 99


# ── The documentation both entry points publish ──────────────────────
#
# `add_route` and `route` declare the same 40 parameters, each with its own
# `Annotated[..., Doc(...)]`. Those strings reach a user through IDE tooltips
# and the generated reference, so whichever entry point their editor resolves
# decides what they read - and four had already drifted, each losing a caveat
# on one side only. The parity test below is the guard.

#: Parameters whose documentation legitimately differs, with the reason. Only
#: `methods` qualifies: `route` defaults it to `GET` and `add_route` does not,
#: so the same sentence would be wrong on one of them.
_DOC_MAY_DIFFER = {"methods"}


def _documented_params(func) -> dict[str, str]:
    """Map each parameter of `func` to the `Doc` text its annotation carries."""
    documented = {}
    for name, hint in typing.get_type_hints(func, include_extras=True).items():
        for meta in getattr(hint, "__metadata__", ()):
            text = getattr(meta, "documentation", None)
            if text is not None:
                documented[name] = text
    return documented


def test_the_two_entry_points_document_a_shared_parameter_identically():
    add_route = _documented_params(Router.add_route)
    route = _documented_params(Router.route)
    drifted = sorted(
        name
        for name in set(add_route) & set(route)
        if name not in _DOC_MAY_DIFFER and add_route[name] != route[name]
    )
    assert not drifted, f"add_route and route document these differently: {drifted}"


def test_every_parameter_route_takes_is_documented_by_both():
    """A parameter documented on one side only is the same loss by another route."""
    add_route = _documented_params(Router.add_route)
    route = _documented_params(Router.route)
    # `handler` is `add_route`'s alone - `route` supplies it by decoration.
    assert set(add_route) - set(route) == {"handler"}
    assert set(route) - set(add_route) == set()


@pytest.mark.parametrize(
    "param",
    [
        "mcp_resource_uri",
        "mcp_resource_mime_type",
        "mcp_meta",
        "mcp_task_support",
        "stream",
        "response_model",
    ],
)
def test_the_longest_docs_are_one_shared_object_rather_than_two_copies(param: str):
    """Identity, not equality: two copies can drift, one object cannot."""

    def doc_object(func):
        hint = typing.get_type_hints(func, include_extras=True)[param]
        return next(m for m in hint.__metadata__ if getattr(m, "documentation", None))

    assert doc_object(Router.add_route) is doc_object(Router.route)


def test_a_shared_doc_still_reaches_the_signature_it_documents():
    """Hoisting must not leave a parameter undocumented."""
    add_route = _documented_params(Router.add_route)
    assert "streaming" in add_route["stream"]
    assert "extension defines" in add_route["mcp_meta"]
    assert "never disagrees" in add_route["mcp_resource_mime_type"]
