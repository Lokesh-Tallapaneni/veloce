"""Tests for the MCP-only plan-extension provider (contrib/mcp/_plan_facts.py)."""

from __future__ import annotations

import inspect

from veloce._handler_plan import build_plan, get_plan_extension
from veloce.contrib.mcp import _plan_facts  # noqa: F401 - import registers the provider
from veloce.contrib.mcp.plan_bridge import build_input_schema


def test_variadic_positional_param_is_reported():
    async def h(*args: int):
        return None

    plan = build_plan(h)
    facts = get_plan_extension(plan, "mcp")
    assert facts.variadic_param == ("args", inspect.Parameter.VAR_POSITIONAL)


def test_variadic_keyword_param_is_reported():
    async def h(**kwargs: int):
        return None

    plan = build_plan(h)
    facts = get_plan_extension(plan, "mcp")
    assert facts.variadic_param == ("kwargs", inspect.Parameter.VAR_KEYWORD)


def test_no_variadic_param_reports_none():
    async def h(a: int):
        return None

    plan = build_plan(h)
    facts = get_plan_extension(plan, "mcp")
    assert facts.variadic_param is None


def test_positional_only_and_variadic_coexist_on_one_plan():
    # positional_only is a direct HandlerPlan field; variadic_param now lives
    # behind the "mcp" extension - both must report correctly for one plan.
    async def h(a: int, /, *args: int):
        return None

    plan = build_plan(h)
    assert plan.positional_only == ("a",)
    facts = get_plan_extension(plan, "mcp")
    assert facts.variadic_param == ("args", inspect.Parameter.VAR_POSITIONAL)


def test_build_input_schema_does_not_crash_for_an_uninspectable_handler():
    # get_plan_extension(plan, "mcp") returns bare None for a handler build_plan
    # itself could not introspect; build_input_schema's `facts is not None`
    # guard must not dereference .variadic_param off that None.
    plan = build_plan(type)  # inspect.signature(type) raises ValueError
    schema = build_input_schema(plan, {})
    assert schema["properties"] == {}
    assert schema.get("required", []) == []
