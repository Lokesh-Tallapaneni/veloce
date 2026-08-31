"""Probes for things that are always present are removed, and stay removed.

Two shapes of the same mistake. `_resolve_route` asked `hasattr(sub_app,
"match")` and `hasattr(sub_app, "handle_request")` before using a mounted app —
but `mount()` sends anything that is not a `Veloce` to `_asgi_mounts` or
`_static_handlers`, so every entry in `_mounted_apps` is a `Veloce` and has both.
The probe was not merely dead: had `handle_request` ever been missing, the loop
fell through to test the *next* mount with this request's body already drained
into a derived sub-request, which is a silent wrong answer where an
`AttributeError` was wanted.

And six settings that `Veloce.__init__` assigns unconditionally were read as
`getattr(app, name, default)` in `contrib/`. Nothing misbehaved — every fallback
happened to match — but the default lived in two files per setting, and the
fallback would have swallowed a rename instead of raising.

These tests freeze the invariants the removals rely on.
"""

from __future__ import annotations

import ast

import pytest

from tests._source import SRC
from veloce import Veloce
from veloce.contrib.mcp.icons import Icon
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.staticfiles import StaticFiles
from veloce.testclient import TestClient

#: Settings assigned unconditionally by `Veloce.__init__` and read directly in
#: `contrib/`. A `getattr` fallback for any of these duplicates its default.
DIRECTLY_READ_SETTINGS = [
    "website_url",
    "mcp_icons",
    "swagger_ui_parameters",
    "swagger_ui_init_oauth",
    "separate_input_output_schemas",
    "disambiguate_operation_ids",
]


# ── the mounted-app invariant ────────────────────────────────────────


def test_only_a_veloce_reaches_the_mounted_app_list():
    """The invariant the removed probes were guessing at."""
    app = Veloce(openapi_url=None)
    inner = Veloce(openapi_url=None)

    async def asgi(scope, receive, send):
        pass

    app.mount("/veloce", inner)
    app.mount("/asgi", asgi)

    assert [type(sub).__name__ for _p, _ps, sub in app._mounted_apps] == ["Veloce"]
    assert all(isinstance(sub, Veloce) for _p, _ps, sub in app._mounted_apps)


def test_a_static_files_mount_does_not_reach_the_mounted_app_list(tmp_path):
    (tmp_path / "assets").mkdir()
    app = Veloce(openapi_url=None)
    app.mount("/assets", StaticFiles(directory=str(tmp_path / "assets")))
    assert app._mounted_apps == []
    assert len(app._static_handlers) == 1


def test_every_mounted_app_has_both_methods():
    app = Veloce(openapi_url=None)
    app.mount("/inner", Veloce(openapi_url=None))
    for _prefix, _slash, sub in app._mounted_apps:
        assert callable(sub.match)
        assert callable(sub.handle_request)


def _mounted_apps_writes(module: str) -> list[int]:
    """Line numbers where `self._mounted_apps` is handed somewhere that writes.

    Read off the AST rather than by counting a substring. The old form asserted
    `source.count("self._mounted_apps, entry") == 1`, so renaming `entry` or
    letting the formatter wrap the call broke a green suite while the invariant
    held, and a second write spelled any other way slipped past.
    """
    tree = ast.parse((SRC / "app" / module).read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # `x._mounted_apps.append(...)` / `.insert(...)` / `.extend(...)`
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"append", "insert", "extend"}
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "_mounted_apps"
        ):
            writes.append(node.lineno)
            continue
        # `self._register_feature_state(self._mounted_apps, entry)` - the list
        # passed to something that appends to it.
        if any(isinstance(arg, ast.Attribute) and arg.attr == "_mounted_apps" for arg in node.args):
            writes.append(node.lineno)
    return writes


def test_mount_has_exactly_one_write_site():
    """Two would let a non-Veloce in through the second one."""
    writes = _mounted_apps_writes("mounting.py")
    assert len(writes) == 1, f"expected one write to `_mounted_apps`, found {writes}"


def test_the_write_scan_would_find_a_second_one(tmp_path):
    """A scan that matched nothing would make the assertion above vacuous."""
    tree = ast.parse(
        "class A:\n"
        "    def f(self):\n"
        "        self._mounted_apps.append(1)\n"
        "        self._register_feature_state(self._mounted_apps, 2)\n"
    )
    found = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"append", "insert", "extend"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_mounted_apps"
            )
            or any(
                isinstance(arg, ast.Attribute) and arg.attr == "_mounted_apps" for arg in node.args
            )
        )
    ]
    assert found == [3, 4]


def _hasattr_probes(module: str) -> list[tuple[int, str]]:
    """Every `hasattr(_, "name")` call in `module`, by line and attribute name.

    The old form searched for the exact text `hasattr(sub_app, "match")`, which
    a rename of the variable or a line wrap would have hidden.
    """
    tree = ast.parse((SRC / "app" / module).read_text(encoding="utf-8"))
    return [
        (node.lineno, node.args[1].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hasattr"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ]


def test_the_probes_are_gone():
    """Dispatch no longer asks a mounted app whether it is one."""
    probed = {name for _line, name in _hasattr_probes("dispatch.py")}
    assert "match" not in probed
    assert "handle_request" not in probed


def test_the_probe_scan_finds_the_ones_that_remain():
    """Not a scan that matches nothing: dispatch does probe other names."""
    assert _hasattr_probes("dispatch.py")


# ── the mounted path still works ─────────────────────────────────────


def test_a_mounted_app_serves_its_route():
    inner = Veloce(openapi_url=None)

    @inner.get("/ping")
    async def ping() -> dict:
        return {"from": "inner"}

    app = Veloce(openapi_url=None)
    app.mount("/inner", inner)
    assert TestClient(app).get("/inner/ping").json() == {"from": "inner"}


def test_a_mounted_app_serves_its_root():
    inner = Veloce(openapi_url=None)

    @inner.get("/")
    async def root() -> dict:
        return {"from": "root"}

    app = Veloce(openapi_url=None)
    app.mount("/inner", inner)
    assert TestClient(app).get("/inner").json() == {"from": "root"}


def test_a_mounted_app_returns_its_own_404():
    inner = Veloce(openapi_url=None)

    @inner.get("/ping")
    async def ping() -> dict:
        return {}

    app = Veloce(openapi_url=None)
    app.mount("/inner", inner)
    assert TestClient(app).get("/inner/nope").status_code == 404


def test_a_post_body_reaches_a_mounted_app():
    """The body is drained into the sub-request; the probe sat right after."""
    inner = Veloce(openapi_url=None)

    @inner.post("/echo")
    async def echo(request) -> dict:
        return await request.json()

    app = Veloce(openapi_url=None)
    app.mount("/inner", inner)
    assert TestClient(app).post("/inner/echo", json={"a": 1}).json() == {"a": 1}


def test_two_mounts_each_serve_their_own():
    first, second = Veloce(openapi_url=None), Veloce(openapi_url=None)

    for inner, label in ((first, "first"), (second, "second")):

        @inner.get("/x")
        async def x(_label=label) -> dict:
            return {"who": _label}

    app = Veloce(openapi_url=None)
    app.mount("/a", first)
    app.mount("/b", second)
    client = TestClient(app)
    assert client.get("/a/x").json() == {"who": "first"}
    assert client.get("/b/x").json() == {"who": "second"}


def test_a_prefix_that_is_a_string_prefix_of_another_is_not_confused():
    """`/a` and `/ab` may coexist; the trailing-slash guard keeps them apart."""
    short, long = Veloce(openapi_url=None), Veloce(openapi_url=None)

    for inner, label in ((short, "short"), (long, "long")):

        @inner.get("/x")
        async def x(_label=label) -> dict:
            return {"who": _label}

    app = Veloce(openapi_url=None)
    app.mount("/a", short)
    app.mount("/ab", long)
    client = TestClient(app)
    assert client.get("/a/x").json() == {"who": "short"}
    assert client.get("/ab/x").json() == {"who": "long"}


def test_a_nested_mount_still_resolves():
    leaf = Veloce(openapi_url=None)

    @leaf.get("/deep")
    async def deep() -> dict:
        return {"ok": True}

    middle = Veloce(openapi_url=None)
    middle.mount("/mid", leaf)
    app = Veloce(openapi_url=None)
    app.mount("/top", middle)
    assert TestClient(app).get("/top/mid/deep").json() == {"ok": True}


# ── the six settings ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", DIRECTLY_READ_SETTINGS)
def test_the_setting_is_always_present(name):
    """A default-less read is only safe because `__init__` always assigns it."""
    assert hasattr(Veloce(openapi_url=None), name)


@pytest.mark.parametrize("name", DIRECTLY_READ_SETTINGS)
def test_the_setting_is_assigned_unconditionally(name):
    """Assigned inside an `if` would make the direct read a latent crash."""
    tree = ast.parse((SRC / "app" / "core.py").read_text(encoding="utf-8"))
    init = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "Veloce"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assigned_at_top_level = {
        target.attr
        for stmt in init.body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Attribute)
    }
    assert name in assigned_at_top_level


@pytest.mark.parametrize("name", DIRECTLY_READ_SETTINGS)
def test_no_contrib_module_re_defaults_the_setting(name):
    """The defect: the default lived in two files per setting."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in (SRC / "contrib").rglob("*.py")
        if f'getattr(app, "{name}"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"{name} is re-defaulted in {offenders}"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("website_url", None),
        ("mcp_icons", None),
        ("swagger_ui_parameters", None),
        ("swagger_ui_init_oauth", None),
        ("separate_input_output_schemas", True),
        ("disambiguate_operation_ids", True),
    ],
)
def test_the_default_is_what_the_removed_fallback_used(name, expected):
    """Removing a fallback must not change what an unconfigured app does."""
    assert getattr(Veloce(openapi_url=None), name) == expected


# ── the settings still reach their consumers ─────────────────────────


def test_swagger_ui_parameters_reach_the_docs_page():
    app = Veloce(swagger_ui_parameters={"docExpansion": "none"})

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert "docExpansion" in TestClient(app).get("/docs").text


def test_disambiguate_operation_ids_is_honoured():
    """Two routes sharing a name collide; the flag decides whether that is fixed."""
    results = {}
    for flag in (True, False):
        app = Veloce(openapi_url=None, disambiguate_operation_ids=flag)

        @app.get("/users/{user_id}", name="lookup")
        async def one(user_id: int) -> dict:
            return {}

        @app.get("/accounts/{account_id}", name="lookup")
        async def two(account_id: int) -> dict:
            return {}

        schema = app.openapi()
        results[flag] = (
            schema["paths"]["/users/{user_id}"]["get"]["operationId"],
            schema["paths"]["/accounts/{account_id}"]["get"]["operationId"],
        )

    first, second = results[True]
    assert first != second
    assert results[False] == ("lookup_get", "lookup_get")


def test_website_url_and_icons_reach_the_mcp_server():

    icon = Icon(src="https://example.test/i.png")
    app = Veloce(openapi_url=None, website_url="https://example.test", mcp_icons=[icon])
    info = MCPServer(app)._server_info()
    assert info["websiteUrl"] == "https://example.test"
    assert info["icons"] == [icon.to_payload()]


def test_an_unset_website_url_is_simply_absent():

    info = MCPServer(Veloce(openapi_url=None))._server_info()
    assert "websiteUrl" not in info
    assert "icons" not in info
