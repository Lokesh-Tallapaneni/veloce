"""Protocol constants and listing helpers — revisions, cache hints, status mapping and entry shapes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

import veloce.status as status
from veloce._internal import _is_async_callable, offload
from veloce.contrib.mcp._helpers import (
    _tool_annotations,
)
from veloce.contrib.mcp.errors import (
    AuthorizationError,
    InvalidParamsError,
    InvalidRequestError,
    MCPCapabilityError,
    MCPError,
    ResourceNotFoundError,
    ToolError,
    _InBandError,
    _InvalidArgumentsError,
)
from veloce.contrib.mcp.icons import render_icons
from veloce.contrib.mcp.registry import (
    VERSION_META_KEY,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import MCPPrompt
    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.resources import MCPResource

MethodHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

# The MCP lifecycle spec returns this from ``initialize`` for an unknown one.
LATEST_PROTOCOL_VERSION = "2025-11-25"

#: The revision before `LATEST_PROTOCOL_VERSION`, still served.
PRIOR_PROTOCOL_VERSION = "2025-06-18"

# Revisions compatible with this ``tools`` surface; ``2025-03-26`` is older.
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({PRIOR_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION})

# The first "modern" revision: no handshake, no session, everything in `_meta`.
MODERN_PROTOCOL_VERSION = "2026-07-28"


def is_modern_version(version: str | None) -> bool:
    """Whether `version` names a revision served in the modern shape.

    The revisions are ISO dates, so ordering them as strings orders them by date
    and a revision later than the first modern one is modern too. The one
    definition both the transport and the core read, so a hop's two ends cannot
    act on different requests.
    """
    return version is not None and version >= MODERN_PROTOCOL_VERSION


# Newest first, echoed verbatim in `server/discover` and in a version error.
SERVED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    MODERN_PROTOCOL_VERSION,
    LATEST_PROTOCOL_VERSION,
    PRIOR_PROTOCOL_VERSION,
)


RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_TASK = "task"
# `ping` owns no capability, so its retirement is declared here instead.
_CORE_HANDSHAKE_ONLY_METHODS = frozenset({"ping"})

# The methods the spec requires caching hints on, for a `complete` result.
_CACHEABLE_METHODS = frozenset(
    {
        "server/discover",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
    }
)
# The registries never change shape after startup, so a generous default is safe.
DEFAULT_CACHE_TTL_MS = 300_000
# A result that can differ between callers must not be cached by a shared proxy.
_CACHE_SCOPE_PUBLIC = "public"
_CACHE_SCOPE_PRIVATE = "private"

_SERVED_VERSION_SET = frozenset(SERVED_PROTOCOL_VERSIONS)

# A prompt or a resource: both are gated by the scopes of the `MCPTool` they carry.
_ScopedT = TypeVar("_ScopedT", bound="MCPPrompt | MCPResource")


def _tool_key(tool: MCPTool) -> str:
    return tool.name


def _resource_key(resource: MCPResource) -> str:
    return resource.uri


def _prompt_key(prompt: MCPPrompt) -> str:
    return prompt.name


def _apply_sync_tool_filter(
    tool_filter: Any, tools: list[MCPTool], principal: Any
) -> list[MCPTool]:
    """Run a synchronous visibility policy over the whole candidate set."""
    return [tool for tool in tools if tool_filter(tool, principal)]


async def _tools_passing_filter(
    tool_filter: Any, tools: list[MCPTool], principal: Any
) -> list[MCPTool]:
    """Run `tool_filter` over `tools`, sync or async.

    The one evaluation path `tools/list` (the whole candidate set) and
    `tools/call` (a list of one) both call, so neither can drift from how
    the other decides. A sync filter is offloaded once for the whole batch,
    including a batch of one: one thread handoff serving every tool, not
    one handoff per tool.
    """
    if _is_async_callable(tool_filter):
        visible = []
        for tool in tools:
            # `_is_async_callable` picked the branch; the alias unions both.
            if await cast("Awaitable[bool]", tool_filter(tool, principal)):
                visible.append(tool)
        return visible
    return cast(
        "list[MCPTool]", await offload(_apply_sync_tool_filter, tool_filter, tools, principal)
    )


def _requested_version(params: dict[str, Any]) -> str | None:
    """Return the tool version this call asked for, if it named one."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    namespaced = meta.get(VERSION_META_KEY)
    if not isinstance(namespaced, dict):
        return None
    version = namespaced.get("version")
    return version if isinstance(version, str) else None


def _build_tool_listing_entry(tool: MCPTool) -> dict[str, Any]:
    """Shape one registered tool into its `tools/list` entry."""
    entry: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
    if tool.title:
        entry["title"] = tool.title
    icons = render_icons(tool.icons)
    if icons is not None:
        entry["icons"] = icons
    # The author's own annotations overlay the verb-derived ones.
    annotations = _tool_annotations(tool.route_methods, tool.title)
    if tool.annotations:
        annotations = {**(annotations or {}), **tool.annotations}
    if annotations:
        entry["annotations"] = annotations
    if tool.output_schema is not None:
        entry["outputSchema"] = tool.output_schema
    # The spec's default is `"forbidden"`, so a non-opting tool omits the field.
    if tool.task_support:
        entry["execution"] = {"taskSupport": "optional"}
    meta = tool.meta
    if tool.version is not None:
        published: dict[str, Any] = {"version": tool.version}
        if tool.version_history:
            published["versions"] = list(tool.version_history)
        declared = meta.get(VERSION_META_KEY) if meta else None
        namespaced = {**declared, **published} if isinstance(declared, dict) else published
        meta = {**meta, VERSION_META_KEY: namespaced} if meta else {VERSION_META_KEY: namespaced}
    if meta:
        entry["_meta"] = meta
    return entry


# The methods whose answer is a listing, and so can be narrowed per connection.
_LIST_METHODS = frozenset(
    {"tools/list", "prompts/list", "resources/list", "resources/templates/list"}
)


def _tool_entry_legacy(tool: MCPTool) -> dict[str, Any]:
    """Return the tool's listing entry, built once and memoized on the tool.

    A pure function of registration data: nothing it reads can change once the
    registry is built.
    """
    entry = tool.listing_entry
    if entry is None:
        entry = tool.listing_entry = _build_tool_listing_entry(tool)
    return entry


def _tool_entry_modern(tool: MCPTool) -> dict[str, Any]:
    """Return the tool's entry for the modern revision, memoized alongside it.

    The modern revision removed `execution` from `Tool`, so an entry carrying the
    field does not validate against the schema that client negotiated. Only a
    task-capable tool differs; every other tool shares the one object.
    """
    modern = tool.listing_entry_modern
    if modern is not None:
        return modern
    entry = _tool_entry_legacy(tool)
    modern = tool.listing_entry_modern = (
        {key: value for key, value in entry.items() if key != "execution"}
        if "execution" in entry
        else entry
    )
    return modern


def _tool_describer(modern: bool) -> Callable[[MCPTool], dict[str, Any]]:
    """Return the describer for an era.

    Chosen once per listing rather than once per tool: reading the era inside the
    describer cost a `ContextVar` lookup and an attribute load for every entry.
    """
    return _tool_entry_modern if modern else _tool_entry_legacy


def _in_band_status_for(error: _InBandError) -> int:
    """Map an in-band failure to the status instrumentation should record."""
    if isinstance(error, _InvalidArgumentsError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(error, MCPCapabilityError):
        return status.HTTP_424_FAILED_DEPENDENCY
    if isinstance(error, ToolError):
        # RFC 9110 Sec. 15.5.10: a declared failure is no server fault.
        return status.HTTP_409_CONFLICT
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _http_status_for(error: MCPError) -> int:
    """Map a raised MCP error to the status instrumentation should record.

    Instrumentation reports what happened in HTTP terms so one dashboard covers
    both doors; the JSON-RPC code the client receives is the error's own.
    """
    if isinstance(error, AuthorizationError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(error, ResourceNotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(error, InvalidParamsError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(error, InvalidRequestError):
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_500_INTERNAL_SERVER_ERROR


class _ServerPageSize:
    """Sentinel type: "use the server's configured page size".

    A distinct type rather than a bare `object()` so the page-size parameter
    narrows to `int | None` once the sentinel is ruled out, and so an explicit
    `page_size=None` - the whole catalogue, unpaged - stays distinguishable from
    "the caller said nothing".
    """

    __slots__ = ()


_SERVER_PAGE_SIZE = _ServerPageSize()
