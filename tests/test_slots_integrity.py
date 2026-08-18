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


@pytest.mark.parametrize("qualname, cls", sorted(_all_veloce_classes().items()))
def test_declared_slots_are_not_defeated_by_an_unslotted_base(qualname: str, cls: type) -> None:
    if not ("__slots__" in cls.__dict__ and not issubclass(cls, BaseException)):
        pytest.skip("no own __slots__")

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


def test_hot_path_objects_are_slotted() -> None:
    """The objects built per request, per connection, or per route match carry
    no per-instance `__dict__` - that is the point of slotting them."""
    from veloce._handler_plan import HandlerPlan
    from veloce.http.datastructures import (
        Cookies,
        FormData,
        Headers,
        QueryParams,
        State,
        UploadFile,
    )
    from veloce.http.request import Request
    from veloce.http.response import Response
    from veloce.routing.router import RouteInfo, RouteMatch
    from veloce.sessions import Session
    from veloce.websocket import WebSocket

    for cls in (
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
    ):
        assert cls.__dictoffset__ == 0, f"{cls.__name__} regained a per-instance __dict__"


def test_contrib_hot_path_objects_are_slotted() -> None:
    """The same holds for the objects contrib builds per message or per event."""
    from veloce.contrib.mcp.context import MCPContext
    from veloce.contrib.mcp.session import MCPSession
    from veloce.contrib.mcp.tasks import MCPTask
    from veloce.http.response import FileResponse
    from veloce.sse import EventSourceResponse, ServerSentEvent

    for cls in (
        MCPSession,
        MCPContext,
        MCPTask,
        ServerSentEvent,
        EventSourceResponse,
        FileResponse,
    ):
        assert cls.__dictoffset__ == 0, f"{cls.__name__} regained a per-instance __dict__"
