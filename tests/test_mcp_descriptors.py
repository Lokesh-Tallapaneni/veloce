"""MCP descriptor hierarchy - shared base fields, subclassing, slot discipline."""

from __future__ import annotations

from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.prompts import MCPPrompt
from veloce.contrib.mcp.registry import MCPTool
from veloce.contrib.mcp.resources import MCPResource


def _tool() -> MCPTool:
    return MCPTool(name="t", description="d", handler=lambda: None, plan=None, input_schema={})


def test_primitives_subclass_the_descriptor_base():
    """Tool, resource, and prompt share `MCPDescriptor` as their base."""
    assert issubclass(MCPTool, MCPDescriptor)
    assert issubclass(MCPResource, MCPDescriptor)
    assert issubclass(MCPPrompt, MCPDescriptor)


def test_base_carries_name_and_description():
    """The base holds the name/description every primitive exposes."""
    descriptor = MCPDescriptor(name="n", description="d")
    assert descriptor.name == "n"
    assert descriptor.description == "d"


def test_tool_inherits_base_fields_and_keeps_its_own():
    """`MCPTool` keeps inherited name/description alongside its distinct fields."""
    tool = _tool()
    assert tool.name == "t"
    assert tool.description == "d"
    assert tool.input_schema == {}
    assert tool.required_scopes == frozenset()


def test_resource_inherits_base_fields():
    """`MCPResource` resolves name/description from the shared base."""
    resource = MCPResource(
        name="r",
        description="d",
        uri="config://app",
        tool=_tool(),
        is_template=False,
        pattern=None,
        uri_param_names=(),
    )
    assert resource.name == "r"
    assert resource.description == "d"
    assert resource.uri == "config://app"


def test_prompt_inherits_base_fields():
    """`MCPPrompt` resolves name/description from the shared base."""
    prompt = MCPPrompt(name="p", description="d", tool=_tool(), arguments=[])
    assert prompt.name == "p"
    assert prompt.description == "d"


def test_subclasses_stay_slotted():
    """Every primitive keeps `__slots__` so it never regains a per-instance dict."""
    assert not hasattr(_tool(), "__dict__")
    resource = MCPResource(
        name="r",
        description="d",
        uri="config://app",
        tool=_tool(),
        is_template=False,
        pattern=None,
        uri_param_names=(),
    )
    assert not hasattr(resource, "__dict__")
    prompt = MCPPrompt(name="p", description="d", tool=_tool(), arguments=[])
    assert not hasattr(prompt, "__dict__")
