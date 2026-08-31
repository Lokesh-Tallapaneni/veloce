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

import ast
import importlib
import inspect
import pathlib
import re

import pytest

# `typing_extensions.get_type_hints`, not `typing`'s: below Python 3.11 the
# stdlib one drops the `Annotated` wrapper whenever the annotated type is a
# union, so `Annotated[dict | None, Doc(...)]` loses its `Doc` and every
# documentation check below reads it as undocumented. The backport carries the
# fix. Runtime dispatch is unaffected - `_handler_plan` does not read
# annotations this way - so this is a property of the introspection, not of the
# framework.
import typing_extensions

from veloce import Blueprint, Request, Router, Veloce
from veloce.routing.router import MCPRouteOptions, RouteInfo
from veloce.testclient import TestClient

#: The modules a route-copying function may live in. `_readd_route` moved from
#: `router.py` to `_internal.py` when the blueprint and router copies were
#: merged, and this guard - which read `router.py` alone - stopped finding it.
#: A guard that only works while the code stays put is not a guard, so the
#: source is now resolved per function rather than pinned to one file.
_SEARCHED_MODULES = (Router.__module__, "veloce._internal")


def _module_source(module_name: str) -> str:
    return pathlib.Path(inspect.getfile(importlib.import_module(module_name))).read_text(
        encoding="utf-8"
    )


_SOURCES = {name: _module_source(name) for name in _SEARCHED_MODULES}

#: Fields that legitimately differ on a re-registered route. The first group is
#: rewritten by the merge itself - the prefix changes the path and name, and the
#: parent's tags, dependencies and responses are combined in. The second is
#: derived from the handler at registration time (`_finalize_plans`), so carrying a
#: stale copy across would be wrong, not right.
#: The MCP constructor parameters, discovered from the signature so this cannot
#: fall behind the code it describes.
_MCP_PARAMETERS = frozenset(
    name
    for name in inspect.signature(Router.add_route).parameters
    if name.startswith(("mcp_", "expose_as_mcp_"))
)

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
    "request_param_name",
    "is_fast_eligible",
    # Derived in `__init__` from the six `response_model_*` flags, which are
    # themselves forwarded, so each copy rebuilds it rather than carrying one.
    # Backed by behaviour tests over both copy paths in
    # `test_response_model_dump_kwargs.py`, not by this exemption alone.
    "response_dump_kwargs",
    # Likewise derived, from `response_model` itself: the `get_origin` result and
    # the backend classification, hoisted out of the per-response path. Both copy
    # paths are covered in `test_response_model_msgspec_shaping.py`.
    "response_model_origin",
    "response_model_backend",
}


def _function_source(function_name: str) -> str:
    """The exact source of the named function, boundaries from the parse tree.

    This used to slice a fixed 4000-character window from the `def` line, which
    overruns both functions it inspects - by 1463 and 545 characters - and so
    reads into whatever follows them. Today the overrun happens to contain no
    `info.<field>` reference, so nothing is falsely credited; the moment a
    neighbouring function gains one, this guard would report a field as carried
    that the copy under test never touches. That is precisely the failure it
    exists to prevent, occurring in the guard itself.
    """
    for source in _SOURCES.values():
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ):
                return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{function_name} not found in {', '.join(_SEARCHED_MODULES)}")


def _forwarded_in(function_name: str) -> set[str]:
    """Names of `RouteInfo` fields the named function reads off the source route.

    Matches every `info.<field>` in the body rather than an assignment shape:
    a field may be wrapped (`list(info.mcp_scopes)`), renamed on the way through
    (`exclude_middleware=info.excluded_middleware`), or assigned after
    construction, and all three of those are still carrying it.
    """
    return set(re.findall(r"\binfo\.([a-z_]+)", _function_source(function_name)))


def test_the_scanned_source_stops_at_the_function():
    """The boundary itself, asserted - a window that overran was the defect.

    The old fixed 4000-character slice reached 545 characters past
    `_build_merged_route_info`, into `_commit_merged_method`. Any `def` other
    than the segment's own means the scan is reading a neighbour, whatever that
    neighbour's indentation - so the count is over `def ` at **any** column, not
    just column zero.
    """
    for name in ("_readd_route", "_build_merged_route_info"):
        body = _function_source(name)
        assert body.startswith(f"def {name}("), name
        others = [
            line.strip()
            for line in body.splitlines()
            if line.lstrip().startswith(("def ", "async def "))
        ]
        assert len(others) == 1, f"{name} scan reached a neighbour: {others[1:]}"


def test_the_scan_finds_the_fields_it_is_meant_to():
    """A boundary fix that returned nothing would make the guard vacuous."""
    for name in ("_readd_route", "_build_merged_route_info"):
        assert len(_forwarded_in(name)) > 10, name


def _carried_fields() -> set[str]:
    """The names a copy has to forward.

    `RouteInfo.__slots__` holds `mcp`, one record standing in for eleven fields
    that used to be slots of their own. A copy forwards it by passing those
    eleven keywords - `RouteInfo.__init__` rebuilds the record - so the names to
    look for are the constructor's, not the slot's.
    """
    fields = {name for name in RouteInfo.__slots__ if not name.startswith("_")}
    fields.discard("mcp")
    fields.update(_MCP_PARAMETERS)
    return fields


@pytest.mark.parametrize("function_name", ["_readd_route", "_build_merged_route_info"])
def test_every_route_field_is_carried_across(function_name: str):
    """The guard: a field neither copy forwards is a silent behaviour change."""
    missing = sorted(_carried_fields() - _REWRITTEN - _forwarded_in(function_name))
    assert not missing, f"{function_name} does not carry: {missing}"


def test_the_mcp_record_stands_in_for_its_eleven_fields():
    """The premise of `_carried_fields`: `mcp` is the only such stand-in, and it
    really does hold every one of them."""
    assert "mcp" in RouteInfo.__slots__
    assert not any(name.startswith("mcp_") for name in RouteInfo.__slots__)
    assert set(_MCP_PARAMETERS) <= set(MCPRouteOptions.__dataclass_fields__)


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
    for name, hint in typing_extensions.get_type_hints(func, include_extras=True).items():
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
        hint = typing_extensions.get_type_hints(func, include_extras=True)[param]
        return next(m for m in hint.__metadata__ if getattr(m, "documentation", None))

    assert doc_object(Router.add_route) is doc_object(Router.route)


def test_a_shared_doc_still_reaches_the_signature_it_documents():
    """Hoisting must not leave a parameter undocumented."""
    add_route = _documented_params(Router.add_route)
    assert "streaming" in add_route["stream"]
    assert "extension defines" in add_route["mcp_meta"]
    assert "never disagrees" in add_route["mcp_resource_mime_type"]


# ── The declaration entry points ─────────────────────────────────────
#
# `_readd_route` and `_build_merged_route_info` are the *copy* paths, guarded
# above. `add_route` and `route` are the *declaration* paths, and they have the
# same failure shape one step earlier: `route` accepts a parameter and forwards
# it to `add_route`, so one it accepts but does not forward is silently dropped
# rather than rejected. Eleven of the forwarded parameters exist for a single
# emit target (MCP), which is where the risk concentrates.


def _forwarded_by_route() -> set[str]:
    """Names `route` passes on to `add_route`.

    Matched as `name=` in the body rather than as an exact call shape, so a
    parameter that is wrapped or renamed on the way through still counts as
    forwarded.
    """
    return set(re.findall(r"^\s*([a-z_]+)=", inspect.getsource(Router.route), re.M))


def test_every_parameter_route_accepts_is_forwarded():
    """One `route` accepts but drops is silent - it is not a `TypeError`."""
    accepted = {name for name in inspect.signature(Router.route).parameters if name != "self"}
    missing = sorted(accepted - _forwarded_by_route() - {"kwargs"})
    assert not missing, f"route accepts but does not forward: {missing}"


def test_the_two_entry_points_accept_the_same_parameters():
    """A parameter on `add_route` alone is unreachable from the decorators."""
    route_params = {name for name in inspect.signature(Router.route).parameters if name != "self"}
    add_params = {
        name
        for name in inspect.signature(Router.add_route).parameters
        if name not in ("self", "handler")
    }
    assert add_params - route_params == set()


def test_every_mcp_parameter_reaches_the_route_info():
    """The eleven single-emit-target fields, end to end through both entry points.

    They are no longer slots - one `mcp` record holds them - so the check is
    that each is readable off a `RouteInfo`, which is the property that actually
    matters to the fifty-six sites in `contrib/mcp` that read them.
    """
    mcp_params = sorted(
        name for name in inspect.signature(Router.add_route).parameters if name.startswith("mcp_")
    )
    assert len(mcp_params) >= 8, mcp_params
    missing = [name for name in mcp_params if not hasattr(RouteInfo, name)]
    assert not missing, missing


def test_an_undeclared_route_reads_every_mcp_field_as_its_default():
    """`RouteInfo.mcp` is `None` for a route that declares nothing, so each
    property has to answer from the defaults rather than raise."""
    app = Veloce(openapi_url=None)

    @app.get("/plain")
    async def plain():
        return {}

    info = app.match("GET", "/plain").route_info
    assert info.mcp is None
    assert info.expose_as_mcp_tool is False
    assert info.expose_as_mcp_resource is False
    assert info.mcp_task_support is False
    assert info.mcp_description is None
    assert info.mcp_scopes is None
    assert info.mcp_icons is None


def test_an_mcp_field_declared_through_the_decorator_reaches_the_tree():
    """The behavioural half: not just that the names line up, but that a value
    set through `@app.get` is the value the tree holds."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://d",
        mcp_description="D",
        mcp_resource_mime_type="text/plain",
        mcp_resource_size=7,
        mcp_meta={"k": "v"},
    )
    async def doc():
        return {}

    info = app.match("GET", "/doc").route_info
    assert info.mcp_resource_mime_type == "text/plain"
    assert info.mcp_resource_size == 7
    assert info.mcp_meta == {"k": "v"}
    assert info.mcp_description == "D"


# ── Fields derived at registration ───────────────────────────────────
#
# `request_param_name` is exempt from the static parity guard because
# `_finalize_plans` recomputes it on every registration path, the same way it
# recomputes `is_request_only_plan` beside it. These are the tests that make the
# exemption honest: a handler that calls its request parameter something other
# than `request` must still be bound correctly through every route-copy path.


def test_a_renamed_request_parameter_is_recorded():
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def route(req: Request):
        return {"path": req.path}

    info = app.match("GET", "/r").route_info
    assert info.is_request_only_plan is True
    assert info.request_param_name == "req"


def test_a_renamed_request_parameter_is_bound_at_dispatch():
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def route(req: Request):
        return {"path": req.path}

    assert TestClient(app).get("/r").json() == {"path": "/r"}


def test_a_renamed_request_parameter_survives_a_blueprint():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/r")
    async def route(req: Request):
        return {"path": req.path}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    assert app.match("GET", "/bp/r").route_info.request_param_name == "req"
    assert TestClient(app).get("/bp/r").json() == {"path": "/bp/r"}


def test_a_renamed_request_parameter_survives_an_included_router():
    router = Router()

    @router.get("/r")
    async def route(req: Request):
        return {"path": req.path}

    app = Veloce(openapi_url=None)
    app.include_router(router, prefix="/api")
    assert app.match("GET", "/api/r").route_info.request_param_name == "req"
    assert TestClient(app).get("/api/r").json() == {"path": "/api/r"}


def test_a_route_with_no_request_parameter_keeps_the_default():
    app = Veloce(openapi_url=None)

    @app.get("/n")
    async def route():
        return {}

    info = app.match("GET", "/n").route_info
    assert info.is_request_only_plan is False
    assert info.request_param_name == "request"


# ── The two decorator signatures declare one option set ──────────────


#: Parameters one signature has and the other legitimately does not.
#: `add_route` takes the handler as an argument where `route` returns a
#: decorator that receives it, and `methods` differs in default because
#: `add_route` is the general form.
SIGNATURE_DIVERGENCES = {"handler", "methods"}


def _annotated_parameters(function_name: str) -> dict[str, str]:
    """Each parameter of the named `Router` method, with its `Doc(...)` text."""
    for source in _SOURCES.values():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != function_name:
                continue
            args = node.args
            found = {}
            for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if argument.arg == "self":
                    continue
                found[argument.arg] = _doc_text(argument.annotation, source)
            return found
    raise AssertionError(f"{function_name} not found in {', '.join(_SEARCHED_MODULES)}")


def _doc_text(annotation, source: str) -> str:
    """The `Doc("...")` string inside an `Annotated[...]`, or "" if there is none."""
    if annotation is None:
        return ""
    for node in ast.walk(annotation):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Doc" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant):
                return str(argument.value)
            # A `_DOC_*` constant: the name is the identity, which is the point.
            return ast.get_source_segment(source, argument) or ""
    return ""


def test_route_and_add_route_declare_the_same_options():
    """The other half of the duplication these tests already guard.

    `route` and `add_route` enumerate ~40 route options each, by hand. The two
    copy functions above are checked field by field because one of them dropped
    `stream` once; nothing checked that the two *signatures* stay in step, and
    an option added to one and not the other is silently unreachable through the
    form the user happened to pick.
    """
    route = _annotated_parameters("route")
    add_route = _annotated_parameters("add_route")

    only_route = set(route) - set(add_route) - SIGNATURE_DIVERGENCES
    only_add_route = set(add_route) - set(route) - SIGNATURE_DIVERGENCES
    assert not only_route, f"`route` declares options `add_route` does not: {sorted(only_route)}"
    assert not only_add_route, (
        f"`add_route` declares options `route` does not: {sorted(only_add_route)}"
    )


def test_the_shared_options_document_themselves_identically():
    """Four parameter docstrings had already drifted, losing a caveat on one side.

    The `_DOC_*` constants repaired seven of them; the rest are still written
    twice, so this holds the two copies to the same text.
    """
    route = _annotated_parameters("route")
    add_route = _annotated_parameters("add_route")

    drifted = [
        f"{name}: route={route[name]!r} add_route={add_route[name]!r}"
        for name in sorted(set(route) & set(add_route) - SIGNATURE_DIVERGENCES)
        if route[name] and add_route[name] and route[name] != add_route[name]
    ]
    assert not drifted, "the same option is documented two ways:\n" + "\n".join(drifted)


def test_the_signature_scan_is_not_vacuous():
    """A scan finding no parameters would satisfy both checks above."""
    route = _annotated_parameters("route")
    assert len(route) > 30, f"only {len(route)} parameters found on `route`"
    assert "stream" in route, "the parameter a copy dropped once is not being seen"
    assert any(route.values()), "no `Doc(...)` text was extracted from any parameter"
