"""Every declared `__slots__` in the package must actually take effect.

A class that declares `__slots__` while inheriting from a base that does not
still gets a per-instance `__dict__`, so the declaration silently buys nothing.
The same holds in reverse for a subclass of a slotted class that omits its own
declaration. Both are invisible in review, so they are asserted here.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys

import pytest

import veloce
from veloce._handler_plan import HandlerPlan
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import MCPTask
from veloce.http.datastructures import (
    Cookies,
    FormData,
    Headers,
    QueryParams,
    State,
    UploadFile,
)
from veloce.http.request import Request
from veloce.http.response import FileResponse, Response
from veloce.routing.router import RouteInfo, RouteMatch
from veloce.sessions import Session
from veloce.sse import EventSourceResponse, ServerSentEvent
from veloce.websocket import WebSocket


def _all_veloce_classes() -> dict[str, type]:
    for module in pkgutil.walk_packages(veloce.__path__, "veloce."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            # Optional integrations (redis, orjson, ...) may be absent.
            continue

    found: dict[str, type] = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("veloce"):
            continue
        for obj in vars(module).values():
            if inspect.isclass(obj) and getattr(obj, "__module__", "").startswith("veloce"):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return found


def _classes_declaring_slots() -> list[tuple[str, type]]:
    """The classes this module has anything to say about.

    Filtered here rather than skipped in the body: parametrizing over every
    class in the package reported 135 skips that meant only "this class has no
    `__slots__`", and a genuine skip would have been invisible among them.
    Exception classes are excluded by the style guide - they do not declare
    `__slots__`.
    """
    return sorted(
        (qualname, cls)
        for qualname, cls in _all_veloce_classes().items()
        if "__slots__" in cls.__dict__ and not issubclass(cls, BaseException)
    )


SLOTTED = _classes_declaring_slots()


def _subclasses_of_slotted_bases() -> list[tuple[str, type]]:
    """Classes inheriting a base that actually manages its layout with slots.

    A base declaring `__slots__ = ()` - `Auditable`, the transport protocols -
    is a marker that adds no storage, and its subclasses are free to carry a
    `__dict__`: middleware is built once at registration, and a user subclass
    setting its own config attributes in `__init__` is the supported shape.
    The rule applies where a base declares real slots, because there the
    subclass silently undoes what the base paid for.
    """
    return sorted(
        (qualname, cls)
        for qualname, cls in _all_veloce_classes().items()
        if not issubclass(cls, BaseException)
        and any(_claims_no_dict(base) for base in cls.__mro__[1:] if base is not object)
    )


def _claims_no_dict(base: type) -> bool:
    """True when `base`'s own `__slots__` is a real, dict-free layout claim.

    Two shapes are not: an empty `__slots__ = ()`, which is a marker adding no
    storage, and one that lists `"__dict__"` - `pydantic.BaseModel` does, so a
    model subclass must *not* declare slots of its own.
    """
    declared = base.__dict__.get("__slots__")
    return bool(declared) and "__dict__" not in declared


INHERITS_SLOTS = _subclasses_of_slotted_bases()


@pytest.mark.parametrize(("qualname", "cls"), INHERITS_SLOTS, ids=[q for q, _c in INHERITS_SLOTS])
def test_a_subclass_of_a_slotted_base_declares_its_own(qualname: str, cls: type) -> None:
    """The reverse of the check below, which the docstring promised and nothing made.

    Without this, dropping `__slots__ = ()` from a subclass does not fail
    anything - the class simply stops being collected by the scan above, so a
    silent regression reads as one fewer test.
    """
    assert "__slots__" in cls.__dict__, (
        f"{qualname} inherits `__slots__` but declares none, so its instances "
        "carry a `__dict__` and the base's declaration buys nothing"
    )


def test_the_scan_found_the_slotted_classes() -> None:
    """A filter that matched nothing would make every case below vanish."""
    assert len(SLOTTED) > 50, f"only {len(SLOTTED)} classes declare __slots__"


@pytest.mark.parametrize(("qualname", "cls"), SLOTTED, ids=[q for q, _c in SLOTTED])
def test_declared_slots_are_not_defeated_by_an_unslotted_base(qualname: str, cls: type) -> None:
    if cls.__dictoffset__ != 0:
        culprits = [
            base.__name__
            for base in cls.__mro__
            if base is not object and "__slots__" not in base.__dict__
        ]
        pytest.fail(
            f"{qualname} declares __slots__ but instances still carry a __dict__; "
            f"add `__slots__ = ()` to: {culprits}"
        )


# The objects built per request, per connection, per route match, per message
# or per event. Naming them explicitly - rather than only checking whatever
# happens to declare `__slots__` - is what makes a *removed* declaration a
# failure rather than one fewer case.
HOT_PATH_CLASSES = [
    Request,
    Response,
    WebSocket,
    Session,
    Headers,
    QueryParams,
    Cookies,
    FormData,
    UploadFile,
    State,
    RouteInfo,
    RouteMatch,
    HandlerPlan,
    MCPSession,
    MCPContext,
    MCPTask,
    ServerSentEvent,
    EventSourceResponse,
    FileResponse,
]


@pytest.mark.parametrize("cls", HOT_PATH_CLASSES, ids=lambda c: c.__name__)
def test_a_hot_path_object_carries_no_dict(cls: type) -> None:
    """No per-instance `__dict__` - that is the point of slotting them."""
    assert cls.__dictoffset__ == 0, f"{cls.__name__} regained a per-instance __dict__"


@pytest.mark.parametrize("cls", HOT_PATH_CLASSES, ids=lambda c: c.__name__)
def test_a_hot_path_object_is_in_the_scan_too(cls: type) -> None:
    """The named list and the package-wide scan must not disagree."""
    assert cls in [scanned for _q, scanned in SLOTTED]
