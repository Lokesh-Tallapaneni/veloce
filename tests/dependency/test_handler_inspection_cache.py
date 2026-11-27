"""A handler planned more than once is inspected once.

`inspect.signature` plus `get_type_hints` is the whole cost of `build_plan`,
and both answers depend only on the callable - so planning the same handler
again, which `include_router` under a path-parameter prefix now does, can reuse
them.

The salvage branch is the exception and the reason this file is careful: when
`get_type_hints` fails, the per-parameter rescue reads `path_params` to decide
whether a dropped annotation is load-bearing, and refuses the registration when
it is. Cached, the same handler would be refused under one route template and
quietly accepted under another.
"""

from __future__ import annotations

import functools
import weakref

import pytest

from veloce import _handler_plan
from veloce._handler_plan import build_plan

# ── the cache hits ──────────────────────────────────────────────────


def test_planning_a_handler_twice_inspects_it_once(monkeypatch):
    calls = []
    real = _handler_plan.get_type_hints

    def counted(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(_handler_plan, "get_type_hints", counted)

    async def handler(item_id: int, q: str = "all"):
        return item_id

    build_plan(handler)
    build_plan(handler, path_params=frozenset({"item_id"}))
    assert len(calls) == 1


def test_two_partials_over_one_function_get_their_own_signatures():
    """The key is the object handed in, not the function it unwraps to."""

    def base(a: int, b: int = 2):
        return a + b

    one_bound = functools.partial(base, 1)
    other_bound = functools.partial(base, b=3)
    assert len(build_plan(one_bound).slots) == 1
    assert len(build_plan(other_bound).slots) == 2


# ── the cache must not hold, and must not hit ───────────────────────


def test_an_unresolvable_annotation_is_re_salvaged_every_time():
    """Tolerated under a template naming it, refused under one that does not.

    This order is the discriminating one: the tolerated call would populate a
    cache, and the refusal would then never happen.
    """

    async def handler(x: NoSuchName):  # noqa: F821
        return x

    with pytest.warns(UserWarning, match="could not resolve"):
        build_plan(handler, path_params=frozenset({"x"}))
    with pytest.raises(TypeError, match="could not resolve"):
        build_plan(handler)


def test_a_callable_that_cannot_be_weak_referenced_still_plans():
    """A builtin takes no weak reference, so the lookup itself must tolerate it.

    Planned twice because the write is what would raise on the first call and
    the read on the second.
    """
    first = build_plan(len)
    second = build_plan(len)
    assert len(first.slots) == len(second.slots)


def test_the_cache_does_not_keep_a_handler_alive():
    """A strong-keyed cache would pin every handler for the process lifetime."""

    async def handler(q: str = "all"):
        return q

    build_plan(handler)
    ref = weakref.ref(handler)
    del handler
    assert ref() is None
