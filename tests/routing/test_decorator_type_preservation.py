"""A decorator must not erase the type of the function it decorates.

Every route and hook decorator here returns the function it was given, so the
honest annotation is a `TypeVar`: a factory returns `Callable[[T], T]` and a
direct decorator is `(func: T) -> T`. Annotating the return as a bare
`Callable` instead says `(...) -> Unknown`, which tells a type checker nothing,
and Pylance/pyright answer with two errors on every decorated handler:

    Type of "post" is partially unknown
      Type of "post" is "(path: str, **kwargs: Any) -> ((...) -> Unknown)"
                                                   (reportUnknownMemberType)
    Untyped function decorator obscures type of function; ignoring decorator
                                            (reportUntypedFunctionDecorator)

The second is the costly one: the checker drops the decorator rather than
reason through it, so the handler keeps its signature only by accident and
anything the decorator *did* express is lost.

These tests read the annotations rather than running pyright, because pyright
is not a dependency of this project. They catch the shape that causes the
errors - a bare `Callable` - which is what regressing this would look like.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from veloce import APIRouter, Blueprint, Veloce

#: Decorator factories: called with arguments, returning the decorator.
FACTORIES = [
    "route",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
]

#: Decorators applied directly to a function, with no call.
DIRECT = [
    "before_request",
    "after_request",
    "teardown_request",
]


def _return_annotation(owner: object, name: str) -> object:
    member = getattr(type(owner), name, None) or getattr(owner, name)
    return inspect.signature(member).return_annotation


def _is_bare_callable(annotation: object) -> bool:
    """Whether an annotation is an unparameterised `Callable`.

    Compared as a string because these modules use PEP 563, so the annotation
    arrives unevaluated. `Callable` alone is the bare form; `Callable[...]`
    carries its parameter and return types and is what a checker can use.
    """
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    return text.strip() in {"Callable", "typing.Callable", "collections.abc.Callable"}


@pytest.mark.parametrize("name", FACTORIES)
def test_a_route_factory_does_not_return_a_bare_callable(name):
    """`@router.post("/x")` must tell the checker what it hands back."""
    annotation = _return_annotation(APIRouter(), name)
    assert not _is_bare_callable(annotation), (
        f"APIRouter.{name} returns a bare `Callable`, so Pylance reports "
        f"reportUntypedFunctionDecorator and ignores the decorator"
    )


@pytest.mark.parametrize("name", DIRECT)
def test_a_hook_decorator_does_not_return_a_bare_callable(name):
    """`@app.before_request` has the same problem in the direct shape."""
    annotation = _return_annotation(Veloce(), name)
    assert not _is_bare_callable(annotation), (
        f"Veloce.{name} returns a bare `Callable`, so a decorated hook loses its type"
    )


def test_a_blueprint_route_factory_is_typed_too():
    """A `Blueprint` is the other route owner users decorate against."""
    annotation = _return_annotation(Blueprint("b", __name__), "get")
    assert not _is_bare_callable(annotation)


def test_an_error_handler_factory_is_typed():
    annotation = _return_annotation(Veloce(), "exception_handler")
    assert not _is_bare_callable(annotation)


# ── the decorators really do return their input ───────────────────────


def test_a_route_decorator_returns_the_function_unchanged():
    """The annotation claims `Callable[[T], T]`; this is what makes it true.

    If a decorator returned a wrapper instead, the `TypeVar` would be a lie a
    checker believes - worse than the bare `Callable` it replaced.
    """
    router = APIRouter()

    async def handler() -> dict[str, int]:
        return {"ok": 1}

    assert router.get("/x")(handler) is handler


def test_a_hook_decorator_returns_the_function_unchanged():
    app = Veloce()

    async def hook() -> None: ...

    assert app.before_request(hook) is hook


def test_an_error_handler_decorator_returns_the_function_unchanged():
    app = Veloce()

    async def handler(request, exc) -> None: ...

    assert app.exception_handler(ValueError)(handler) is handler


def test_the_decorated_handler_keeps_its_own_signature():
    """What the user actually loses when the decorator is ignored.

    `get_type_hints` on the decorated name must still describe the handler, so
    an editor can complete and check calls against it.
    """
    router = APIRouter()

    @router.post("/items")
    async def create_item(name: str) -> dict[str, str]:
        return {"name": name}

    hints = typing.get_type_hints(create_item)
    assert hints["name"] is str
    assert hints["return"] == dict[str, str]
