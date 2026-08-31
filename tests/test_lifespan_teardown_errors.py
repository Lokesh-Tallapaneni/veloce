"""Every lifespan-teardown failure is surfaced, not just the last one.

`AsyncExitStack.aclose()` runs every teardown, chains each failure onto the
previous through `__context__`, and re-raises the last. Reporting only what it
raised would hide every earlier failure - so `_collect_chained` walks the chain,
and `_run_shutdown` / `_shutdown_subapps` aggregate what it finds.

Neither the walk nor either of its two call sites was executed by the suite: a
recovery path with no test is one that is only exercised when something has
already gone wrong.

Note the `try/finally` in every lifespan below - the shape `add_lifespan`'s own
docstring shows, and the shape that matters here. A generator that simply raises
after its `yield` never reaches that code when the stack throws an exception in
at the yield point, so its teardown does not run at all and there is nothing to
aggregate. Writing them without it, this module tested the opposite of what it
claims.
"""

from __future__ import annotations

import contextlib
import sys

import pytest

from veloce import Veloce
from veloce._internal import _BaseExceptionGroup
from veloce.app.lifecycle import _collect_chained


def _chain(*excs: BaseException) -> BaseException:
    """Chain `excs` through `__context__` the way `aclose()` does."""
    for previous, current in zip(excs, excs[1:], strict=False):
        current.__context__ = previous
    return excs[-1]


class TestCollectChained:
    def test_a_lone_exception_is_returned_alone(self) -> None:
        exc = ValueError("only")
        assert _collect_chained(exc) == [exc]

    def test_the_chain_is_returned_oldest_first(self) -> None:
        """Matching the order the teardowns ran, which is what the docstring says."""
        first, second, third = ValueError("1"), KeyError("2"), RuntimeError("3")
        assert _collect_chained(_chain(first, second, third)) == [first, second, third]

    def test_a_self_referential_chain_terminates(self) -> None:
        """The cycle guard, on the input that would otherwise hang the walk."""
        exc = ValueError("loop")
        exc.__context__ = exc
        assert _collect_chained(exc) == [exc]

    def test_a_two_element_cycle_terminates(self) -> None:
        first, second = ValueError("a"), ValueError("b")
        first.__context__ = second
        second.__context__ = first
        assert _collect_chained(first) == [second, first]

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="BaseExceptionGroup is 3.11+")
    def test_an_interior_group_is_expanded(self) -> None:
        """Its members are surfaced individually rather than as one entry."""
        inner_a, inner_b = ValueError("a"), ValueError("b")
        group = _BaseExceptionGroup("both", [inner_a, inner_b])
        tail = RuntimeError("tail")
        collected = _collect_chained(_chain(group, tail))
        assert collected == [inner_a, inner_b, tail]

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="BaseExceptionGroup is 3.11+")
    def test_a_group_at_the_head_is_expanded_too(self) -> None:
        inner = ValueError("inner")
        assert _collect_chained(_BaseExceptionGroup("one", [inner])) == [inner]


class TestShutdownAggregatesEveryTeardownFailure:
    """The call sites: two teardowns fail, and both are reported."""

    @staticmethod
    def _app_with_two_failing_teardowns() -> Veloce:
        app = Veloce(openapi_url=None)

        @app.add_lifespan
        @contextlib.asynccontextmanager
        async def first(_app):
            try:
                yield
            finally:
                raise ValueError("first teardown failed")

        @app.add_lifespan
        @contextlib.asynccontextmanager
        async def second(_app):
            try:
                yield
            finally:
                raise KeyError("second teardown failed")

        return app

    async def test_both_failures_reach_the_caller(self) -> None:
        app = self._app_with_two_failing_teardowns()
        await app._run_lifecycle("startup")
        with pytest.raises(BaseException) as excinfo:  # noqa: B017 - group or bare
            await app._run_lifecycle("shutdown")

        raised = excinfo.value
        messages = " ".join(
            str(e) for e in (getattr(raised, "exceptions", None) or [raised])
        ) + " ".join(str(n) for n in getattr(raised, "__notes__", ()))
        assert "first teardown failed" in messages
        assert "second teardown failed" in messages

    async def test_a_single_failure_is_re_raised_as_itself(self) -> None:
        """Documented: one failure keeps its traceback verbatim."""
        app = Veloce(openapi_url=None)

        @app.add_lifespan
        @contextlib.asynccontextmanager
        async def only(_app):
            try:
                yield
            finally:
                raise ValueError("the only one")

        await app._run_lifecycle("startup")
        with pytest.raises(ValueError, match="the only one"):
            await app._run_lifecycle("shutdown")

    async def test_a_clean_shutdown_raises_nothing(self) -> None:
        app = Veloce(openapi_url=None)
        ran: list[str] = []

        @app.add_lifespan
        @contextlib.asynccontextmanager
        async def clean(_app):
            try:
                yield
            finally:
                ran.append("torn down")

        await app._run_lifecycle("startup")
        await app._run_lifecycle("shutdown")
        assert ran == ["torn down"]


class TestSubappShutdownAggregates:
    """The second call site: a mounted sub-app whose shutdown raises."""

    async def test_every_subapp_is_torn_down_even_when_one_raises(self) -> None:
        parent = Veloce(openapi_url=None)
        torn: list[str] = []

        def child(name: str, *, fail: bool) -> Veloce:
            app = Veloce(openapi_url=None)

            @app.add_lifespan
            @contextlib.asynccontextmanager
            async def teardown(_app):
                try:
                    yield
                finally:
                    torn.append(name)
                    if fail:
                        raise RuntimeError(f"{name} failed")

            return app

        parent.mount("/a", child("a", fail=True))
        parent.mount("/b", child("b", fail=False))

        await parent._run_lifecycle("startup")
        with pytest.raises(BaseException):  # noqa: B017 - the aggregate
            await parent._run_lifecycle("shutdown")

        assert set(torn) == {"a", "b"}, "a failing child stopped the others"
