"""Deriving one tool from another — a narrower façade over a tool that exists.

An internal tool is often nearly the tool you want to publish, differing only in
what an agent should see: a friendlier argument name, a credential the caller
must not supply, a limit the caller must not raise. Rewriting the handler to get
that is duplication, and wrapping it by hand means restating the schema.

`derive_tool` builds a new tool from a registered one, changing only the
arguments' surface. The original keeps working and keeps its own registration;
the derived tool calls the same handler with the arguments translated back.

    public = derive_tool(
        internal, name="search",
        arguments={
            "query": ArgTransform(name="q", description="What to search for"),
            "api_key": ArgTransform(hide=True, default=SERVER_KEY),
            "limit": ArgTransform(default=10),
        },
    )
    app.add_mcp_tool(public)

A hidden argument disappears from the published schema and is supplied on every
call, which is how a server-held credential stays server-held while the tool that
needs it is still exposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool

# Marks "no default given", so that `default=None` can mean the default is None.
_UNSET = object()


@dataclass(frozen=True, slots=True)
class ArgTransform:
    """How one argument of a derived tool differs from the original's.

    `name` publishes the argument under a different name; the value is translated
    back before the handler runs. `description` and `schema` reshape what the
    agent is told. `default` supplies a value when the caller omits one. `hide`
    removes the argument from the schema entirely, in which case `default` is
    what the handler receives - a hidden argument with no default would leave the
    handler short of something it requires, so that combination is refused.
    """

    name: str | None = None
    description: str | None = None
    schema: dict[str, Any] | None = None
    default: Any = _UNSET
    hide: bool = False
    required: bool | None = None

    def __post_init__(self) -> None:
        if self.hide and self.default is _UNSET:
            raise ValueError(
                "a hidden argument needs a default: the caller cannot supply it, "
                "so without one the handler would be called without it"
            )
        if self.hide and self.name is not None:
            raise ValueError("a hidden argument is not published, so renaming it does nothing")

    @property
    def has_default(self) -> bool:
        """Whether this transform supplies a value when the caller omits one."""
        return self.default is not _UNSET


@dataclass(frozen=True, slots=True)
class _Binding:
    """How one published argument maps back to the original's."""

    original: str
    published: str | None
    default: Any = _UNSET

    @property
    def has_default(self) -> bool:
        return self.default is not _UNSET


def derive_tool(
    tool: MCPTool,
    *,
    name: str | None = None,
    description: str | None = None,
    arguments: dict[str, ArgTransform] | None = None,
) -> MCPTool:
    """Return a new tool that calls `tool`'s handler through a narrower surface.

    `arguments` maps an argument of the original to how it should appear. An
    argument not mentioned is published unchanged. Naming one the original does
    not have is refused, since it would silently do nothing.
    """

    transforms = arguments or {}
    schema = tool.input_schema or {}
    properties: dict[str, Any] = dict(schema.get("properties") or {})
    required: set[str] = set(schema.get("required") or ())

    unknown = sorted(set(transforms) - set(properties))
    if unknown:
        raise ValueError(
            f"{tool.name!r} has no argument(s) {unknown}; it takes {sorted(properties) or 'none'}"
        )

    published: dict[str, Any] = {}
    published_required: list[str] = []
    bindings: list[_Binding] = []

    for original, definition in properties.items():
        transform = transforms.get(original)
        if transform is None:
            published[original] = definition
            if original in required:
                published_required.append(original)
            bindings.append(_Binding(original=original, published=original))
            continue
        if transform.hide:
            bindings.append(_Binding(original=original, published=None, default=transform.default))
            continue

        entry = dict(transform.schema) if transform.schema is not None else dict(definition)
        if transform.description is not None:
            entry["description"] = transform.description
        published_name = transform.name or original
        published[published_name] = entry

        is_required = original in required
        if transform.has_default:
            is_required = False
        if transform.required is not None:
            is_required = transform.required
        if is_required:
            published_required.append(published_name)
        bindings.append(
            _Binding(
                original=original,
                published=published_name,
                default=transform.default if transform.has_default else _UNSET,
            )
        )

    derived_schema: dict[str, Any] = {**schema, "properties": published}
    if published_required:
        derived_schema["required"] = published_required
    else:
        derived_schema.pop("required", None)

    return replace(
        tool,
        name=name or tool.name,
        description=description or tool.description,
        input_schema=derived_schema,
        derived_from=_Derivation(tuple(bindings)),
    )


@dataclass(frozen=True, slots=True)
class _Derivation:
    """The argument translation a derived tool applies before its handler runs."""

    bindings: tuple[_Binding, ...] = field(default=())

    def translate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Map the published arguments back to what the original handler takes."""
        translated: dict[str, Any] = {}
        for binding in self.bindings:
            if binding.published is None:
                translated[binding.original] = binding.default
                continue
            if binding.published in arguments:
                translated[binding.original] = arguments[binding.published]
            elif binding.has_default:
                translated[binding.original] = binding.default
        return translated
