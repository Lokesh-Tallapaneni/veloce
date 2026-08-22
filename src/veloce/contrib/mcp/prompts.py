"""MCP prompt registry — reusable prompt templates an agent can fetch.

A prompt is the Model Context Protocol primitive for a parameterised message
template a user invokes (the third primitive alongside tools and resources).
Register one with ``@app.mcp_prompt(...)``: the decorated callable's parameters
become the prompt's arguments, and its return value - a string, or a list of
role/content messages - becomes the messages ``prompts/get`` returns. The callable
runs through the same invocation path a pure ``@app.mcp_tool`` does, so ``Depends``
and ``MCPContext`` work inside a prompt exactly as in a tool.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from veloce._handler_plan import build_plan
from veloce.contrib.mcp._registry_base import Registry
from veloce.contrib.mcp.composition import mcp_mounts, renamed
from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.icons import Icon, coerce_icons
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.contrib.mcp.registry import MCPTool
from veloce.contrib.mcp.safety import require_mcp_description


@dataclass(slots=True)
class MCPPrompt(MCPDescriptor):
    """One registered MCP prompt template."""

    # The prompt callable wrapped as an `MCPTool` (no route), so a ``prompts/get``
    # replays it through the shared pure-tool invocation path - resolving its
    # `Depends` graph and `MCPContext` exactly as a tool call does.
    tool: MCPTool
    # The prompt's declared arguments (name + required + optional description),
    # derived from the callable's agent-supplied parameters. MCP prompt arguments
    # are strings, so only the name/required/description are advertised - never a
    # JSON type.
    arguments: list[dict[str, Any]]


@dataclass(slots=True)
class PromptRegistry(Registry[MCPPrompt]):
    """Name -> `MCPPrompt`, plus the shared JSON Schema component registry."""

    prompts: dict[str, MCPPrompt] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def _store(self) -> dict[str, MCPPrompt]:
        return self.prompts

    def _key(self, item: MCPPrompt) -> str:
        return item.name

    def _duplicate_message(self, key: str) -> str:
        return (
            f"Duplicate MCP prompt name {key!r}. Prompt names must be unique; "
            "rename the handler, pass name=, or set namespace=."
        )

    def add(self, prompt: MCPPrompt) -> None:
        """Register `prompt`, rejecting a name already taken."""
        self.register(prompt)


def _prompt_arguments(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the MCP prompt argument list from a handler input schema.

    A prompt argument carries only a name, a required flag, and an optional
    description; MCP prompt arguments are always strings, so the JSON type the
    input schema records is not advertised.
    """
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", ()))
    arguments: list[dict[str, Any]] = []
    for name, prop in properties.items():
        argument: dict[str, Any] = {"name": name, "required": name in required}
        description = prop.get("description")
        if description:
            argument["description"] = description
        arguments.append(argument)
    return arguments


def _register_prompt(
    registry: PromptRegistry,
    handler: Callable,
    *,
    name: str | None,
    description: str | None,
    namespace: str | None,
    scopes: frozenset[str] | None = None,
    icons: Sequence[Icon] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Add an `@app.mcp_prompt`-registered handler to `registry`."""
    base = name or handler.__name__
    prompt_name = f"{namespace}_{base}" if namespace else base
    desc = require_mcp_description(prompt_name, description)
    plan = build_plan(handler)
    input_schema = build_input_schema(plan, registry.schemas)
    tool = MCPTool(
        name=prompt_name,
        description=desc,
        handler=handler,
        plan=plan,
        input_schema=input_schema,
        required_scopes=scopes or frozenset(),
    )
    registry.add(
        MCPPrompt(
            name=prompt_name,
            description=desc,
            tool=tool,
            arguments=_prompt_arguments(input_schema),
            icons=coerce_icons(icons),
            meta=meta,
        )
    )


def build_prompt_registry(app: Any) -> PromptRegistry:
    """Assemble the prompt registry from `@app.mcp_prompt` registrations."""
    registry = PromptRegistry()
    for handler, name, description, namespace, scopes, icons, meta in getattr(
        app, "_mcp_prompts", ()
    ):
        _register_prompt(
            registry,
            handler,
            name=name,
            description=description,
            namespace=namespace,
            scopes=scopes,
            icons=icons,
            meta=meta,
        )
    for namespace, sub_app in mcp_mounts(app):
        for prompt in build_prompt_registry(sub_app).prompts.values():
            registry.add(renamed(prompt, namespace))

    return registry
