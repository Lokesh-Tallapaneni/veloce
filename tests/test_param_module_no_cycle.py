"""The parameter markers are reachable without initialising the routing package.

`routing/router.py` could not import `_handler_plan` at module scope:

    # local: breaks the routing.router <-> _handler_plan cycle.
    from veloce._handler_plan import K_REQUEST, build_plan, build_route_dep_plans

The comment was true, and the cycle was real — hoisting the import raised
`ImportError`. The chain:

    router  ->  _handler_plan  ->  veloce.routing.params
                                      | importing a submodule initialises its package
                                      v
                                   routing/__init__  ->  routing.router   (half-built)

`routing/params.py` imports nothing from veloce — it is already a leaf. It was
simply imprisoned inside a package whose `__init__` imports the heavy `router`,
so **every** module that wanted a parameter marker had to drag `router` in with
it. Seven modules did, four of which sit above `routing` in the dependency
direction.

The markers now live in `veloce/_params.py`, a neutral leaf that imports nothing
from veloce. The cycle is gone at its cause rather than deferred around, and
`router` imports `_handler_plan` at module scope like any other module.

`veloce/routing/params.py` is gone rather than kept as a re-export shim. The
seven markers are public through `veloce.__all__` and through `veloce.routing`;
the module path was documented nowhere, so by this project's own definition of
the public surface it was never public API, and a second file existing only to
redirect is the redundancy this change is about.

This file pins the whole contract: one class object per marker however you reach
it, every marker still binding end to end, and the import working in any order.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from pathlib import Path as _Path

import pytest
from pydantic import BaseModel

import veloce
from veloce import Body, Cookie, File, Form, Header, Path, Query, Veloce
from veloce.testclient import TestClient

SRC = _Path(__file__).resolve().parents[1] / "src"
MARKERS = ["Body", "Cookie", "File", "Form", "Header", "Path", "Query", "ParamBase"]


class Payload(BaseModel):
    """Module scope: this file uses PEP 563, so a local class cannot resolve."""

    note: str


def _in_fresh_interpreter(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a new interpreter, so import order is genuinely fresh."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        cwd=str(SRC.parent),
    )


# ── the cycle is gone ────────────────────────────────────────────────


def test_the_router_imports_the_handler_plan_at_module_scope():
    """The defect: this had to be deferred into a method body."""
    source = (SRC / "veloce/routing/router.py").read_text(encoding="utf-8")
    body_start = source.index("class ")
    header = source[:body_start]
    assert "from veloce._handler_plan import" in header


def test_no_comment_still_claims_the_cycle():
    source = (SRC / "veloce/routing/router.py").read_text(encoding="utf-8")
    assert "routing.router <-> _handler_plan" not in source


def test_the_marker_module_imports_nothing_from_veloce():
    """What makes it a safe leaf for anything to depend on."""
    source = (SRC / "veloce/_params.py").read_text(encoding="utf-8")
    offenders = [
        line for line in source.splitlines() if line.startswith(("from veloce", "import veloce"))
    ]
    assert offenders == []


def test_the_marker_module_loads_with_no_veloce_package_at_all():
    """The property that removes the cycle: it is a true leaf.

    Loaded straight from its file, with `veloce` absent from `sys.modules`, so
    nothing about the package - least of all `routing/__init__` importing
    `router` - can be involved. That is what makes it safe for `_handler_plan`
    to depend on.
    """
    result = _in_fresh_interpreter(
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location('leaf', r'"
        + str(SRC / "veloce/_params.py").replace("\\", "\\\\")
        + "'); "
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
        "print(mod.Query is not None and not [m for m in sys.modules if m.startswith('veloce')])"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ── importing works in any order ─────────────────────────────────────


@pytest.mark.parametrize(
    "first",
    [
        "veloce",
        "veloce.routing.router",
        "veloce._params",
        "veloce._handler_plan",
        "veloce.routing",
        "veloce.views",
        "veloce.contrib.openapi",
    ],
)
def test_importing_this_module_first_works(first):
    """A cycle shows up as an order dependency; there must be none."""
    result = _in_fresh_interpreter(f"import {first}; import veloce; print('ok')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_handler_plan_can_be_imported_alone():
    result = _in_fresh_interpreter(
        "import veloce._handler_plan as p; "
        "print(callable(p.build_plan) and isinstance(p.K_REQUEST, int))"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ── one class object per marker, whichever path reaches it ───────────


@pytest.mark.parametrize("name", MARKERS)
def test_every_import_path_yields_the_same_class(name):
    """A second copy would break every `isinstance` check in the resolver."""
    import veloce._params as leaf
    import veloce.routing as routing

    objects = [getattr(leaf, name)]
    for module in (veloce, routing):
        if hasattr(module, name):
            objects.append(getattr(module, name))
    assert all(obj is objects[0] for obj in objects)


@pytest.mark.parametrize("name", ["Body", "Cookie", "File", "Form", "Header", "Path", "Query"])
def test_a_marker_is_still_a_parambase(name):
    from veloce._params import ParamBase

    assert issubclass(getattr(veloce, name), ParamBase)


def test_the_redirect_only_module_is_gone():
    """Undocumented, and a file that only redirects is the redundancy removed."""
    assert not (SRC / "veloce/routing/params.py").exists()


def test_nothing_in_the_tree_still_imports_the_removed_path():
    """An *import*, not a mention.

    This file is skipped: it names the removed path in its own prose and in the
    needles just below, so it would always match itself.
    """
    here = pathlib.Path(__file__).resolve()
    needles = ("from veloce.routing.params " + "import", "import veloce.routing.params")
    for directory in ("veloce", "../tests"):
        for path in (SRC / directory).rglob("*.py"):
            if path.resolve() == here:
                continue
            source = path.read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in source, f"{path}: {needle}"


def test_the_routing_package_still_re_exports_the_markers():
    import veloce.routing as routing

    for name in ["Body", "Cookie", "File", "Form", "Header", "Path", "Query"]:
        assert getattr(routing, name) is getattr(veloce, name), name


def test_the_routing_package_still_exposes_the_router():
    from veloce.routing import RouteInfo, RouteMatch, Router

    assert all(isinstance(obj, type) for obj in (Router, RouteInfo, RouteMatch))


# ── every marker still binds, end to end ─────────────────────────────


def _app() -> TestClient:
    app = Veloce(title="Params", version="1.0.0")

    @app.get("/q")
    async def q(limit: int = Query(default=5, ge=1, le=100)) -> dict:
        return {"limit": limit}

    @app.get("/p/{item_id}")
    async def p(item_id: int = Path()) -> dict:
        return {"item_id": item_id}

    @app.post("/b")
    async def b(note: str = Body(embed=True)) -> dict:
        return {"note": note}

    @app.post("/f")
    async def f(note: str = Form()) -> dict:
        return {"note": note}

    @app.get("/h")
    async def h(x_trace: str = Header(default="none")) -> dict:
        return {"trace": x_trace}

    @app.get("/c")
    async def c(session: str = Cookie(default="anon")) -> dict:
        return {"session": session}

    return TestClient(app)


def test_a_query_marker_binds():
    assert _app().get("/q?limit=9").json() == {"limit": 9}


def test_a_query_marker_applies_its_constraint():
    assert _app().get("/q?limit=999").status_code == 422


def test_a_query_marker_uses_its_default():
    assert _app().get("/q").json() == {"limit": 5}


def test_a_path_marker_binds():
    assert _app().get("/p/7").json() == {"item_id": 7}


def test_a_body_marker_binds():
    assert _app().post("/b", json={"note": "hi"}).json() == {"note": "hi"}


def test_a_bare_model_body_still_validates():
    """The documented form, and the one the resolver reshapes."""
    app = Veloce(openapi_url=None)

    @app.post("/m")
    async def m(payload: Payload) -> dict:
        return {"note": payload.note}

    assert TestClient(app).post("/m", json={"note": "hi"}).json() == {"note": "hi"}


def test_a_form_marker_binds():
    assert _app().post("/f", data={"note": "hi"}).json() == {"note": "hi"}


def test_a_header_marker_binds():
    assert _app().get("/h", headers={"X-Trace": "abc"}).json() == {"trace": "abc"}


def test_a_cookie_marker_binds():
    client = _app()
    client.set_cookie("session", "s1")
    assert client.get("/c").json() == {"session": "s1"}


def test_a_file_marker_binds():
    """`File()` goes through the multipart path, so it gets its own app."""
    from veloce import UploadFile

    app = Veloce(openapi_url=None)

    @app.post("/upload")
    async def upload(document: UploadFile = File()) -> dict:
        return {"size": len(await document.read())}

    response = TestClient(app).post("/upload", files={"document": ("a.txt", b"hello")})
    assert response.json() == {"size": 5}


# ── the lowerings that read markers still work ───────────────────────


def test_the_markers_reach_the_openapi_document():
    """`contrib/openapi` imports `ParamBase`; a second copy would silently skip."""
    app = Veloce(title="Params", version="1.0.0")

    @app.get("/q")
    async def q(limit: int = Query(default=5, ge=1, le=100)) -> dict:
        return {"limit": limit}

    parameters = app.openapi()["paths"]["/q"]["get"]["parameters"]
    assert [p["name"] for p in parameters] == ["limit"]
    assert parameters[0]["schema"]["maximum"] == 100


def test_a_marker_reaches_an_mcp_tool_schema():
    """The other lowering that classifies markers."""
    app = Veloce(title="Params", version="1.0.0", openapi_url=None)

    @app.get("/q", expose_as_mcp_tool=True, mcp_description="Query")
    async def q(limit: int = Query(default=5, ge=1, le=100)) -> dict:
        return {"limit": limit}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "p", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    schema = listed["result"]["tools"][0]["inputSchema"]
    # Only that the marker reached the lowering. Whether its `le=100` reaches the
    # published schema is a separate defect, tracked in OPEN-FINDINGS.
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["limit"]["default"] == 5


def test_a_method_view_still_refuses_a_marker():
    """`views.py` imports `ParamBase`; the refusal depends on that isinstance."""
    from veloce import MethodView, Request

    with pytest.raises(TypeError, match="cannot resolve"):

        class Bad(MethodView):
            async def get(self, request: Request, q: str = Query(default="")) -> dict:
                return {}


def test_oauth2_still_builds_its_form_scheme():
    """`security/oauth2.py` imports `Form` from the same module."""
    from veloce.security.oauth2 import OAuth2PasswordRequestForm

    assert OAuth2PasswordRequestForm is not None
