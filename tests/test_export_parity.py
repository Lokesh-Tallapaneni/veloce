"""Executable export-parity guard for the public surface.

`.claude/rules/development-guardrails.md` states the `__all__` placement and
gateway-parity rules in prose. This module turns them into assertions:

1. every name in the top-level ``__all__`` resolves;
2. every name a subpackage gateway publishes is either at the top level too, or
   recorded in ``SUBPACKAGE_ONLY`` with the reason it stays one level down;
3. every name a gateway ``__init__`` imports appears in that gateway's
   ``__all__`` (the guardrails call the alternative "always a bug");
4. no leaf module declares ``__all__``;
5. every public class or function defined in a top-level leaf module is either
   exported or recorded in ``UNEXPORTED`` with the reason it is not.

The two mappings are the point of the module. A name that is deliberately not
public has to be written down with a reason, so the next reader inherits a
decision instead of an open question.

Complements ``tests/test_public_surface.py``: that file freezes *which* names
are public, this one enforces the *rules* about where they live.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import veloce

PACKAGE_ROOT = pathlib.Path(veloce.__file__).parent

# The gateways whose surface is expected to mirror the top level. Gateways
# nested under `contrib` are excluded: guardrails L173 exempts `contrib` from
# the re-export rule because its integrations are optional-dependency islands.
MIRRORED_GATEWAYS = (
    "veloce.app",
    "veloce.http",
    "veloce.middleware",
    "veloce.routing",
    "veloce.security",
    "veloce.serving",
)


# Names a subpackage gateway publishes that deliberately stop there.
SUBPACKAGE_ONLY: dict[str, str] = {
    # `veloce.http` — header vocabulary, not `Request` attribute types. The five
    # types a handler annotates against (`Headers`, `QueryParams`, `Cookies`,
    # `State`, `Address`) are top level; these are parsing helpers a caller
    # reaches for only when working with a header outside a request.
    "CacheControl": "header value object, not a Request attribute type",
    "HeaderSet": "header value object, not a Request attribute type",
    "header_key": "raw ASGI header-list helper, used when building responses",
    "header_get": "raw ASGI header-list helper, used when building responses",
    "header_present": "raw ASGI header-list helper, used when building responses",
    "header_pop": "raw ASGI header-list helper, used when building responses",
    "parse_multipart_form": "standalone parser; `request.form` is the normal entry point",
    # `veloce.routing` — route-table introspection types. Returned by the
    # router, never constructed by application code.
    "RouteInfo": "router introspection type; not constructed by applications",
    "RouteMatch": "router introspection type; not constructed by applications",
    # `veloce.serving` — the raw-transport protocol object. Reached through
    # `app.run()`; a caller wiring it by hand is already below the public layer.
    "HttpProtocol": "raw-transport internals; `app.run()` is the public entry point",
}


# Public classes and functions defined in a top-level leaf module that are
# deliberately not exported anywhere.
UNEXPORTED: dict[str, str] = {
    # `audit.py` — public, but deliberately module-qualified. A bare `run` in the
    # top-level namespace would read as the server (`Veloce.run`); the audit is
    # called as `veloce.audit.run(app)`, which says what it runs.
    "audit.run": "public as veloce.audit.run; a top-level `run` would collide with Veloce.run",
    # `cli.py` — console-script entry points, reached through `[project.scripts]`
    # by dotted string, never imported by application code.
    "cli.build_parser": "console-script entry point, referenced by dotted path",
    "cli.main": "console-script entry point, referenced by dotted path",
    # `json_provider.py` — the shared JSON encoders. Framework-internal: every
    # surface that sends JSON to a client routes through them so an
    # application's dialect cannot reach some and miss others. Public-named
    # rather than underscored because `app/` imports them across a subpackage
    # boundary, which guardrails forbid for a private symbol.
    "json_provider.resolve_dumps": "internal hot-path resolver, crosses a subpackage boundary",
    # `debug.py` — the debug traceback renderer, wired by the app when
    # `DEBUG` is set.
    "debug.render_traceback_html": "internal debug-page renderer wired by the app",
    # `dependency.py` — the resolver behind `Depends()`. Users declare
    # dependencies; they do not drive the resolver.
    "dependency.DependencyResolver": "DI engine; `Depends`/`Security` are the public surface",
    # `encoders.py` — the orjson `default=` hook. `register_encoder` is the
    # public way to extend encoding.
    "encoders.orjson_default": "orjson hook; `register_encoder` is the public extension point",
    # `exceptions.py` — the status-code lookup behind `abort()`.
    "exceptions.exception_for_status": "lookup behind `abort()`; not user vocabulary",
    # `exceptions.py` — the 501 class is `ServerNotImplemented`, which is exported.
    # `NotImplemented_` stays bound to it for code written against the old
    # spelling; it is an alias rather than a definition, so it is not a name this
    # scan reports, and it is recorded here only so the intent is written down.
    # `metrics.py` / `otel.py` — optional-dependency instrumentors. Measured:
    # importing them costs ~72 ms and ~32 ms on top of `import veloce`, so they
    # stay deep and every user who never touches observability pays nothing.
    "metrics.instrument_with_prometheus": "optional dependency; ~72 ms import cost kept off `import veloce`",
    "otel.instrument_with_otel": "optional dependency; ~32 ms import cost kept off `import veloce`",
    # `signals.py` — `connect()` defaults to `ANY_SENDER`, so the constant is
    # only needed for an explicit per-sender subscription. The eight signal
    # instances plus `Signal` and `Namespace` are top level.
    "signals.ANY_SENDER": "connect() defaults to it; reachable as veloce.signals.ANY_SENDER",
    # `ratelimit.py` — the decorator's TypeVar, part of `rate_limit`'s signature
    # rather than a name callers write.
    "ratelimit.T_handler": "TypeVar in the `rate_limit` signature, not a callable surface",
    # `status.py` — a predicate over the status constants, used by the response
    # encoders to decide whether a body may be emitted.
    "status.status_permits_body": "internal predicate used by the response encoders",
    # `testclient.py` — the response type `TestClient` returns. Reachable as
    # `veloce.testclient.TestResponse` for a typed test helper.
    "testclient.TestResponse": "test-only type; reachable as veloce.testclient.TestResponse",
    # `websocket.py` — handshake internals. `WebSocket` is the public object.
    "websocket.compute_accept": "RFC 6455 handshake internals",
    "websocket.WebSocketState": "connection-state enum used by the dispatch core",
    "websocket.build_listener_handler": "internal handler factory",
    # `workers.py` — the gunicorn worker class, named to gunicorn by dotted
    # string on the command line, never imported.
    "workers.VeloceWorker": "gunicorn worker, referenced by dotted path",
    "workers.build_protocol_factory": "gunicorn worker internals",
    "workers.build_ssl_context": "gunicorn worker internals",
    # ── app/ mixins ──────────────────────────────────────────────
    # `Veloce` is composed of these; they are never constructed or imported
    # by application code, and `veloce.app` exports only `Veloce` itself.
    "app.asgi.AsgiMixin": "composition unit of Veloce; not constructed by application code",
    "app.background.BackgroundTasksMixin": "composition unit of Veloce; not constructed by application code",
    "app.dispatch.DispatchMixin": "composition unit of Veloce; not constructed by application code",
    "app.errors.ErrorsMixin": "composition unit of Veloce; not constructed by application code",
    "app.introspection.IntrospectionMixin": "composition unit of Veloce; not constructed by application code",
    "app.lifecycle.LifecycleMixin": "composition unit of Veloce; not constructed by application code",
    "app.mcp.MCPMixin": "composition unit of Veloce; not constructed by application code",
    "app.middleware.MiddlewareMixin": "composition unit of Veloce; not constructed by application code",
    "app.mounting.MountingMixin": "composition unit of Veloce; not constructed by application code",
    "app.openapi.OpenAPIMixin": "composition unit of Veloce; not constructed by application code",
    "app.plugins.PluginsMixin": "composition unit of Veloce; not constructed by application code",
    "app.serving.ServingMixin": "composition unit of Veloce; not constructed by application code",
    "app.templating.TemplatingMixin": "composition unit of Veloce; not constructed by application code",
    "app.testing.TestingMixin": "composition unit of Veloce; not constructed by application code",
    # ── contrib/mcp internals ────────────────────────────────────
    # Reached across the MCP package's own submodules. `contrib` is exempt
    # from the re-export rule (guardrails L173), so these stay module-local.
    "contrib.mcp.completion.attach_completers": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.composition.T": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.composition.mount_namespace": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.composition.renamed": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.composition.mcp_mounts": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.descriptors.MCPDescriptor": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.errors.parse_error": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.errors.invalid_request_error": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.errors.internal_error": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.errors.UnsupportedProtocolVersionError": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.icons.coerce_icons": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.icons.render_icons": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.pagination.T": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.pagination.encode_cursor": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.pagination.decode_cursor": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.pagination.paginate": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.plan_bridge.build_input_schema": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.plan_bridge.build_output_schema": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.plan_bridge.bind_arguments": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.safety.require_mcp_description": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.safety.TOOL_ANNOTATION_HINTS": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.safety.validate_tool_annotations": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.sampling.content_blocks": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.server.is_modern_version": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.subscriptions.resource_updated_notification": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.subscriptions.resources_list_changed_notification": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.subscriptions.subscription_acknowledged_notification": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.subscriptions.subscription_closed_response": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.subscriptions.ConnectionRegistry": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.tasks.TASK_STATUSES": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.tasks.task_ttl_ms": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.tasks.new_task": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.tasks.create_task_result": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.tasks.status_notification": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.toolsearch.ToolStep": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.toolsearch.ToolSearch": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.transports.event_store.SSEEventStore": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.transports.http.register_metadata_route": "internal to the MCP integration; not part of its published surface",
    "contrib.mcp.transports.session_store.HttpSessionStore": "internal to the MCP integration; not part of its published surface",
    # ── routing/converters concrete classes ──────────────────────
    # `veloce.routing` publishes the `Converter` base and `register_converter`;
    # the built-in converters are implementations reached through that seam,
    # and the parsing helpers serve the router alone.
    "routing.converters.StringConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.IntConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.FloatConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.UUIDConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.PathConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.DateConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.DateTimeConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.TimeConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.TimeDeltaConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.DecimalConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.AnyConverter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.parse_converter": "implementation behind the Converter base / register_converter seam",
    "routing.converters.is_regex_path": "implementation behind the Converter base / register_converter seam",
    "routing.converters.extract_regex_converters": "implementation behind the Converter base / register_converter seam",
    "routing.converters.build_route_regex": "implementation behind the Converter base / register_converter seam",
    "routing.converters.path_param_schemas": "implementation behind the Converter base / register_converter seam",
    # ── remaining leaf internals ─────────────────────────────────
    "contrib.openapi.SchemaRegistry": "internal accumulator for one schema build",
    "routing.router.MCPRouteOptions": "the record behind RouteInfo.mcp; read through its properties",
    "app.mcp.MCPToolRegistration": "one @app.mcp_tool registration, read at mount time",
    "app.mcp.MCPPromptRegistration": "one @app.mcp_prompt registration, read at mount time",
    "app.mcp.MCPCompleterRegistration": "one @app.mcp_completer registration, read at mount time",
    "contrib.docs_ui.SWAGGER_HTML": "template body for the built-in docs route",
    "contrib.docs_ui.REDOC_HTML": "template body for the built-in docs route",
    "exceptions.http_exception_payload": "shared error-body builder for the in-tree emit paths",
    "http.cookies.iter_cookies": "the single cookie parser; users read request.cookies",
    "http.cookies.parse_cookie": "header-level helper behind Request.cookies",
    "http.cookies.dump_cookie": "header-level helper behind Response.set_cookie",
    "http.dates.http_date": "header-level date formatting behind the response helpers",
    "http.dates.parse_date": "header-level date parsing behind the request helpers",
    "http.response.header_pop": "raw header-list helper used by the emit paths",
    "middleware.base.Auditable": "protocol the startup audit checks middleware against",
    "routing.router.RadixNode": "the routing tree's node; an internal data structure",
    "routing.router.RegexRoute": "the regex fallback's route record; internal",
    "serving.reloader.is_reloader_child": "reloader process-role probe, used by the runner",
    "serving.reloader.run_with_reloader": "reloader entry point, reached through Veloce.run",
}


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _declared_all(tree: ast.Module) -> list[str] | None:
    """Return the literal `__all__` of a parsed module, or None if absent."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            value = node.value
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                return [e.value for e in value.elts if isinstance(e, ast.Constant)]
            return []
    return None


#: Modules whose names are typing machinery rather than package surface. A
#: gateway that resolves an export lazily needs `TYPE_CHECKING` and `Any` to do
#: it, and neither is something a user imports from the package.
_NON_SURFACE_MODULES = frozenset({"__future__", "typing"})


def _imported_names(tree: ast.Module) -> set[str]:
    """Public names a module binds through a top-level `from X import Y`.

    Only a module-level import binds a module attribute, which is what makes a
    name reachable as `veloce.X` and therefore what `__all__` has to account
    for. An import inside a function binds a local and is invisible either way,
    so it is not part of the surface this checks.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module not in _NON_SURFACE_MODULES:
            for alias in node.names:
                bound = alias.asname or alias.name
                if not bound.startswith("_"):
                    names.add(bound)
    return names


def _defined_public_names(tree: ast.Module) -> list[str]:
    """Public classes, functions, and constructed module-level singletons.

    A plain constant assignment (`HTTP_200_OK = 200`) is not a surface name; a
    constructed one (`request_started = Signal(...)`) is, so the rule keys on
    the value being a call.
    """
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.target, ast.Name)
        ):
            names.append(node.target.id)
    return [n for n in names if not n.startswith("_")]


def test_every_toplevel_export_resolves():
    missing = [name for name in veloce.__all__ if not hasattr(veloce, name)]
    assert not missing, f"declared in veloce.__all__ but not importable: {missing}"


def test_toplevel_all_has_no_duplicates():
    seen = sorted({n for n in veloce.__all__ if veloce.__all__.count(n) > 1})
    assert not seen, f"duplicated in veloce.__all__: {seen}"


def test_subpackage_exports_reach_the_top_level():
    top = set(veloce.__all__)
    unrecorded: list[str] = []
    for gateway in MIRRORED_GATEWAYS:
        module = importlib.import_module(gateway)
        for name in module.__all__:
            if name not in top and name not in SUBPACKAGE_ONLY:
                unrecorded.append(f"{gateway}.{name}")
    assert not unrecorded, (
        "published by a subpackage gateway but neither top level nor recorded "
        f"in SUBPACKAGE_ONLY with a reason: {unrecorded}"
    )


def test_subpackage_only_entries_are_still_published():
    """A recorded exception that no gateway publishes any more is stale."""
    published: set[str] = set()
    for gateway in MIRRORED_GATEWAYS:
        published |= set(importlib.import_module(gateway).__all__)
    stale = sorted(name for name in SUBPACKAGE_ONLY if name not in published)
    assert not stale, f"SUBPACKAGE_ONLY records names no gateway publishes: {stale}"


def test_gateway_imports_are_all_exported():
    """A name imported into a gateway but absent from `__all__` is invisible."""
    invisible: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("__init__.py")):
        tree = _parse(path)
        declared = _declared_all(tree)
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        assert declared is not None, f"{rel}: gateway module declares no __all__"
        for name in sorted(_imported_names(tree) - set(declared)):
            invisible.append(f"{rel}:{name}")
    assert not invisible, f"imported into a gateway but not in its __all__: {invisible}"


def test_no_leaf_module_declares_all():
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if path.name != "__init__.py" and _declared_all(_parse(path)) is not None
    ]
    assert not offenders, f"leaf modules must not declare __all__: {offenders}"


def _gateway_names(rel: pathlib.Path) -> set[str]:
    """Names exported by the nearest package gateway above `rel`.

    The repository rule is that an `__init__.py` gateway owns the public
    surface, so a leaf name re-exported by its own subpackage gateway is
    published - it does not additionally have to appear in the top-level
    `__all__`.
    """
    parts = rel.parts[:-1]
    package = "veloce" + ("." + ".".join(parts) if parts else "")
    try:
        module = importlib.import_module(package)
    except Exception:  # pragma: no cover - an optional integration's deps
        return set()
    return set(getattr(module, "__all__", ()) or ())


def test_leaf_module_public_names_are_exported_or_recorded():
    """Every public leaf name is exported through a gateway or written down.

    This walked `PACKAGE_ROOT.glob("*.py")` - not `rglob` - so it enforced the
    rule on the package root only and exempted all eight subpackages, while the
    sibling `__all__` guard two functions above already used `rglob`. Eighty-three
    public names across `app/`, `contrib/`, `http/`, `middleware/`, `routing/`
    and `serving/` had never been through this decision.
    """
    top = set(veloce.__all__)
    unrecorded: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        rel = path.relative_to(PACKAGE_ROOT).with_suffix("")
        # A private subpackage (`_internal`-style) is out of scope entirely.
        if any(part.startswith("_") for part in rel.parts):
            continue
        dotted = ".".join(rel.parts)
        gateway = _gateway_names(rel)
        for name in _defined_public_names(_parse(path)):
            if name in top or name in gateway or f"{dotted}.{name}" in UNEXPORTED:
                continue
            unrecorded.append(f"{dotted}.{name}")
    assert not unrecorded, (
        "public in a leaf module but neither exported nor recorded in "
        f"UNEXPORTED with a reason: {unrecorded}"
    )


def test_unexported_entries_are_still_defined():
    """A recorded exception whose symbol is gone, or now exported, is stale.

    Walks the same tree as the guard it complements, so a subpackage entry is
    validated rather than silently accepted.
    """
    top = set(veloce.__all__)
    defined: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        rel = path.relative_to(PACKAGE_ROOT).with_suffix("")
        if any(part.startswith("_") for part in rel.parts):
            continue
        dotted = ".".join(rel.parts)
        defined |= {f"{dotted}.{n}" for n in _defined_public_names(_parse(path))}
    stale = sorted(key for key in UNEXPORTED if key not in defined or key.rsplit(".", 1)[1] in top)
    assert not stale, f"UNEXPORTED records names that are gone or now exported: {stale}"


# The names promoted to the top level in this change, keyed by the module that
# defines them. `from veloce import X` must be the *same object* as the deep
# import, so a handler registered against one matches an instance of the other.
PROMOTED: dict[str, tuple[str, ...]] = {
    "veloce.exceptions": (
        "VeloceError",
        "BadRequest",
        "Unauthorized",
        "PaymentRequired",
        "Forbidden",
        "NotFound",
        "MethodNotAllowed",
        "NotAcceptable",
        "ProxyAuthenticationRequired",
        "RequestTimeout",
        "Conflict",
        "Gone",
        "LengthRequired",
        "PreconditionFailed",
        "RequestEntityTooLarge",
        "RequestURITooLong",
        "UnsupportedMediaType",
        "RangeNotSatisfiable",
        "ExpectationFailed",
        "ImATeapot",
        "UnprocessableEntity",
        "TooManyRequests",
        "InternalServerError",
        "ServerNotImplemented",
        "BadGateway",
        "ServiceUnavailable",
        "GatewayTimeout",
    ),
    "veloce.http.datastructures": ("QueryParams", "Cookies", "State", "Address"),
    "veloce.signals": (
        "Signal",
        "Namespace",
        "request_started",
        "request_finished",
        "request_tearing_down",
        "got_request_exception",
        "message_flashed",
        "appcontext_pushed",
        "appcontext_popped",
        "appcontext_tearing_down",
    ),
    "veloce.health": ("HealthPlugin",),
}


def test_promoted_names_are_the_same_object_as_the_deep_import():
    mismatched: list[str] = []
    for module_name, names in PROMOTED.items():
        module = importlib.import_module(module_name)
        for name in names:
            if getattr(veloce, name) is not getattr(module, name):
                mismatched.append(f"{module_name}.{name}")
    assert not mismatched, f"top-level export is a different object: {mismatched}"


def test_promoted_names_are_all_exported():
    top = set(veloce.__all__)
    missing = [n for names in PROMOTED.values() for n in names if n not in top]
    assert not missing, f"promoted but absent from veloce.__all__: {missing}"


def test_server_not_implemented_aliases_the_underscore_spelling():
    from veloce.exceptions import NotImplemented_

    assert veloce.ServerNotImplemented is NotImplemented_


def test_request_attribute_types_are_all_top_level():
    """The five types a handler annotates against live in one place.

    Guards the premise as well as the export: each name must actually be the
    runtime type of the `Request` attribute it is claimed to describe.
    """
    request = veloce.Request(
        "GET",
        "/",
        "a=1",
        {"cookie": "k=v"},
        b"",
        scope={"client": ("127.0.0.1", 5000)},
    )
    attribute_types = {
        "headers": request.headers,
        "query_params": request.query_params,
        "cookies": request.cookies,
        "state": request.state,
        "client": request.client,
    }
    top = set(veloce.__all__)
    for attribute, value in attribute_types.items():
        name = type(value).__name__
        assert name in top, f"request.{attribute} is a {name}, which is not top level"
