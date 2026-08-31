"""An intermediate class may leave a required method to its own subclasses.

`View` and `Converter` refuse a subclass that does not supply the methods the
base declares - a forgotten `dispatch_request` becomes an import-time
`TypeError` rather than a `NotImplementedError` on the first request to the
route it was registered for.

That refusal also catches the class between the base and the concrete view: an
`AuthenticatedView` holding shared behaviour, completed by the views beneath it.
The pattern is supported - `_require_methods` exempts `__abstractmethods__`, so
marking the method `@abc.abstractmethod` declares the intent - but nothing
covered it and the refusal message did not mention it, so a reader was told what
was missing without being told that leaving it out was allowed.
"""

from __future__ import annotations

import abc
import warnings

import pytest

from veloce.routing.converters import Converter
from veloce.views import View


def test_an_unmarked_intermediate_view_is_refused():
    """The check's reason to exist: a genuinely forgotten method."""
    with pytest.raises(TypeError, match="dispatch_request"):

        class Forgotten(View):
            __slots__ = ()


def test_the_refusal_names_the_supported_pattern():
    """A reader hitting this should not have to read the framework to continue."""
    with pytest.raises(TypeError, match="abstractmethod"):

        class Forgotten(View):
            __slots__ = ()


def test_an_abstract_intermediate_view_is_allowed():
    """The shape a shared base actually takes."""

    class AuthenticatedView(View, abc.ABC):
        __slots__ = ()

        async def check_auth(self) -> bool:
            return True

        @abc.abstractmethod
        async def dispatch_request(self): ...

    assert AuthenticatedView.__abstractmethods__ == frozenset({"dispatch_request"})


def test_a_concrete_subclass_of_an_abstract_intermediate_works():
    """The intermediate is only useful if what it carries reaches the leaf."""

    class AuthenticatedView(View, abc.ABC):
        __slots__ = ()

        async def check_auth(self) -> bool:
            return True

        @abc.abstractmethod
        async def dispatch_request(self): ...

    class Profile(AuthenticatedView):
        __slots__ = ()

        async def dispatch_request(self):
            return {"ok": True}

    assert callable(Profile.as_view("profile"))


def test_an_abstract_intermediate_converter_is_allowed():
    """`Converter` runs the same check, so it takes the same escape."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        class BaseConverter(Converter, abc.ABC):
            __slots__ = ()

            @abc.abstractmethod
            def match(self, value: str): ...

    assert "match" in BaseConverter.__abstractmethods__


def test_an_unmarked_intermediate_converter_is_still_refused():
    with pytest.raises(TypeError, match="match"), warnings.catch_warnings():
        warnings.simplefilter("ignore")

        class Forgotten(Converter):
            __slots__ = ()
