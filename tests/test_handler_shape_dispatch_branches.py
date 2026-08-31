"""Both handler-shape dispatch branches keep real coverage.

`build_plan` sorts a route into one of two fast paths (`routing/router.py`):

    is_trivial_plan       the handler takes no parameters at all
    is_request_only_plan  the handler takes only `request`, and no dependencies

They are different code: the trivial path calls the handler with no arguments,
where the request-only path binds `kwargs = {"request": request}` and skips
`DependencyResolver`. A suite that stops exercising one of them loses the
ability to tell them apart.

That is a live risk rather than a theoretical one, because 257 route handlers in
this suite take `request` and never read it. Every one is an ARG001 hit, and
deleting the parameter looks like tidying - it is not. It moves the route from
`is_request_only_plan` to `is_trivial_plan`, and 33 modules would stop touching
the request-only branch altogether, among them
`test_native_refusal_response_phase.py` and `test_streaming_response_asgi.py`,
which are about what dispatch does *around* the handler.

Renaming to `_request` is not a way out either: `build_plan` classifies the slot
by the parameter's NAME, so an underscore prefix flips the branch exactly as
deletion does while looking like a pure lint fix.

So the unused parameters stay, and this module is why. It holds the split as a
floor: a sweep fails here, with the reason, instead of silently halving the
coverage of a dispatch path.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent

#: Decorators that register a route.
ROUTE_DECORATORS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "websocket", "query"}
)

#: Floors, not targets. Each may rise; neither may fall without a reason.
#: Measured at 523 request-only and 1113 trivial handlers across the suite.
REQUEST_ONLY_HANDLER_FLOOR = 450
TRIVIAL_HANDLER_FLOOR = 1000
REQUEST_ONLY_MODULE_FLOOR = 100


def _is_route_decorator(node: ast.expr) -> bool:
    """Whether a decorator expression registers a route."""
    call = node.func if isinstance(node, ast.Call) else node
    return isinstance(call, ast.Attribute) and call.attr in ROUTE_DECORATORS


def _handler_shapes(path: pathlib.Path) -> tuple[int, int]:
    """`(trivial, request_only)` handler counts in one module.

    By AST rather than by regex: several modules keep whole app sources inside
    triple-quoted strings, which a textual scan would count as real handlers.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
        return (0, 0)

    trivial = request_only = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_route_decorator(d) for d in node.decorator_list):
            continue
        arguments = [
            a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        ]
        if not arguments:
            trivial += 1
        elif arguments == ["request"]:
            request_only += 1
    return (trivial, request_only)


_SHAPES = {path: _handler_shapes(path) for path in sorted(TESTS.glob("test_*.py"))}
_TRIVIAL = sum(trivial for trivial, _ in _SHAPES.values())
_REQUEST_ONLY = sum(request_only for _, request_only in _SHAPES.values())
_REQUEST_ONLY_MODULES = [path.name for path, (_, n) in _SHAPES.items() if n]


def test_the_request_only_branch_is_still_exercised():
    """The floor a sweep of the unused `request` parameters would break."""
    assert _REQUEST_ONLY >= REQUEST_ONLY_HANDLER_FLOOR, (
        f"only {_REQUEST_ONLY} handlers take `request` alone, below the floor of "
        f"{REQUEST_ONLY_HANDLER_FLOOR}. Deleting an unused `request` parameter moves "
        "its route from `is_request_only_plan` to `is_trivial_plan` - a different "
        "dispatch path - so removing them as unused arguments costs coverage rather "
        "than tidying. Renaming to `_request` does the same thing, because the slot "
        "is classified by the parameter's name."
    )


def test_the_trivial_branch_is_still_exercised():
    """The other half, so the split cannot collapse in either direction."""
    assert _TRIVIAL >= TRIVIAL_HANDLER_FLOOR, (
        f"only {_TRIVIAL} handlers take no parameters, below the floor of {TRIVIAL_HANDLER_FLOOR}"
    )


def test_the_request_only_branch_is_covered_broadly():
    """One module's worth of request-only handlers is not coverage of a dispatch path."""
    assert len(_REQUEST_ONLY_MODULES) >= REQUEST_ONLY_MODULE_FLOOR, (
        f"only {len(_REQUEST_ONLY_MODULES)} modules exercise `is_request_only_plan`, "
        f"below the floor of {REQUEST_ONLY_MODULE_FLOOR}"
    )


def test_the_two_branches_are_really_different_paths():
    """The premise. If these ever became one flag, the floors above would be theatre."""
    # Deferred deliberately: importing here keeps this premise check
    # independent of whatever the module's other tests import.
    from veloce.routing.router import RouteInfo

    assert "is_trivial_plan" in RouteInfo.__slots__
    assert "is_request_only_plan" in RouteInfo.__slots__


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("@app.get('/x')\nasync def h():\n    pass\n", (1, 0), id="trivial"),
        pytest.param(
            "@app.get('/x')\nasync def h(request):\n    pass\n", (0, 1), id="request-only"
        ),
        pytest.param(
            "@app.get('/x')\nasync def h(request, q: int = 0):\n    pass\n", (0, 0), id="neither"
        ),
        pytest.param("async def h(request):\n    pass\n", (0, 0), id="not-a-route"),
    ],
)
def test_the_classifier_sorts_a_handler_correctly(tmp_path, source, expected):
    """A scan that classified nothing would satisfy neither floor, but say so wrongly."""
    module = tmp_path / "probe.py"
    module.write_text(source, encoding="utf-8")
    assert _handler_shapes(module) == expected
