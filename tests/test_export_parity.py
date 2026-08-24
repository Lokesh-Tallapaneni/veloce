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
    "json_provider.dumps_for": "internal shared encoder, crosses a subpackage boundary",
    "json_provider.dumps_current": "internal shared encoder, crosses a subpackage boundary",
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


def test_leaf_module_public_names_are_exported_or_recorded():
    top = set(veloce.__all__)
    unrecorded: list[str] = []
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        stem = path.stem
        for name in _defined_public_names(_parse(path)):
            if name in top or f"{stem}.{name}" in UNEXPORTED:
                continue
            unrecorded.append(f"{stem}.{name}")
    assert not unrecorded, (
        "public in a leaf module but neither exported nor recorded in "
        f"UNEXPORTED with a reason: {unrecorded}"
    )


def test_unexported_entries_are_still_defined():
    """A recorded exception whose symbol is gone, or now exported, is stale."""
    top = set(veloce.__all__)
    defined: set[str] = set()
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        defined |= {f"{path.stem}.{n}" for n in _defined_public_names(_parse(path))}
    stale = sorted(key for key in UNEXPORTED if key not in defined or key.split(".", 1)[1] in top)
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
