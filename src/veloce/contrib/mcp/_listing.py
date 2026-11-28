"""Catalogue listing — tools, resources and prompts as paged JSON-RPC results."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp._helpers import (
    _describe_prompt,
    _describe_resource,
    _describe_resource_template,
    _era_modern_var,
    _principal_lacks_scopes,
)
from veloce.contrib.mcp.context import _session_var
from veloce.contrib.mcp.pagination import paginate
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import PromptRegistry
    from veloce.contrib.mcp.registry import MCPTool, ToolFilter, ToolRegistry
    from veloce.contrib.mcp.resources import ResourceRegistry
    from veloce.contrib.mcp.toolsearch import ToolSearch
from veloce.contrib.mcp._protocol import (
    _SERVER_PAGE_SIZE,
    _prompt_key,
    _resource_key,
    _ScopedT,
    _ServerPageSize,
    _tool_describer,
    _tool_key,
    _tools_passing_filter,
)

_logger = logging.getLogger(__name__)


class ListingMixin:
    """Catalogue listing behaviour of `MCPServer`, mixed in so the server module stays readable."""

    # `MCPServer` is slotted, so a mixin without this regains a `__dict__`.
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes the host `MCPServer` assigns in `__init__`.
        registry: ToolRegistry
        resources: ResourceRegistry
        prompts: PromptRegistry
        _tool_filter: ToolFilter | None
        _tool_search: ToolSearch | None
        _any_scoped_tools: bool
        _any_scoped_resources: bool
        _any_scoped_prompts: bool
        _page_size: int | None

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # With no scopes, no filter and no search catalogue there is nothing to narrow.
        tools: Iterable[MCPTool] = (
            self.registry.tools.values()
            if self._tool_filter is None
            and self._tool_search is None
            and not self._any_scoped_tools
            else await self._visible_tools()
        )
        describe = _tool_describer(_era_modern_var.get())
        return self._listing("tools", tools, _tool_key, describe, params)

    def _resource_listing(
        self, params: dict[str, Any], page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE
    ) -> dict[str, Any]:
        """Build the `resources/list` result.

        Called by the JSON-RPC handler and by `MCPContext.list_resources`, so a
        handler enumerating the catalogue cannot contradict the client's own
        listing, nor drop either narrowing: the caller's scopes and the
        connection's hidden set.
        """
        return self._listing(
            "resources",
            self._readable(self.resources.statics(), self._any_scoped_resources),
            _resource_key,
            _describe_resource,
            params,
            page_size=page_size,
        )

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._resource_listing(params)

    async def _handle_resource_templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._listing(
            "resourceTemplates",
            self._readable(self.resources.templates(), self._any_scoped_resources),
            _resource_key,
            _describe_resource_template,
            params,
        )

    def _prompt_listing(
        self, params: dict[str, Any], page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE
    ) -> dict[str, Any]:
        """Build the `prompts/list` result. See `_resource_listing`."""
        return self._listing(
            "prompts",
            self._readable(self.prompts.prompts.values(), self._any_scoped_prompts),
            _prompt_key,
            _describe_prompt,
            params,
            page_size=page_size,
        )

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._prompt_listing(params)

    @staticmethod
    def _readable(items: Iterable[_ScopedT], any_scoped: bool) -> Iterable[_ScopedT]:
        """Drop the resources / prompts whose scopes the calling principal lacks.

        The same check `resources/read` and `prompts/get` perform, so a primitive is
        never listed to a caller that would be refused it. `any_scoped` is decided
        once at construction: a server declaring no scopes walks nothing.
        """
        if not any_scoped:
            return items
        return [item for item in items if not _principal_lacks_scopes(item.tool.required_scopes)]

    def _listing(
        self,
        key: str,
        items: Iterable[Any],
        key_of: Callable[[Any], str],
        describe: Callable[[Any], dict[str, Any]],
        params: dict[str, Any],
        page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Shape one page of a catalogue into its list result.

        Paging happens before shaping, so a page costs the entries it emits rather
        than the whole catalogue, and `nextCursor` is present only while more
        remain. `page_size` overrides the server's own; `None` returns the whole
        catalogue unpaged, which is what the `MCPContext` listings want.
        """
        # Applied before paging, so a hidden entry never occupies a slot on a page.
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        if hidden:
            items = [item for item in items if key_of(item) not in hidden]
        if isinstance(page_size, _ServerPageSize):
            size: int | None = self._page_size
        else:
            size = page_size
        page, cursor = paginate(items, key_of, params.get("cursor"), size)
        result: dict[str, Any] = {key: [describe(item) for item in page]}
        if cursor is not None:
            result["nextCursor"] = cursor
        return result

    def _unnarrowed_tools(self) -> dict[str, MCPTool] | None:
        """Return the registry's own name -> tool map when nothing narrows it here.

        A declared scope, a configured `tool_filter` and what this connection hid
        can each narrow the catalogue. With none in play the candidate set *is* the
        registry, fixed once the server is built; `None` when the full walk has to
        run instead.
        """
        if self._tool_filter is not None or self._any_scoped_tools:
            return None
        session = _session_var.get()
        if session is not None and session.hidden:
            return None
        return self.registry.tools

    async def _visible_tools(self) -> list[MCPTool]:
        """List the tools `tools/list` reports to the calling principal.

        The narrowed catalogue, unless the server publishes its catalogue through
        search - in which case the listing is the three search tools and every
        other tool is found through them.
        """
        search = self._tool_search
        if search is None:
            return await self._candidate_tools()
        # Narrowing the whole catalogue only to discard it has no reader.
        if self._unnarrowed_tools() is not None:
            return list(search.tools)
        return [tool for tool in await self._candidate_tools() if tool.name in search.names]

    async def _candidate_tools(self) -> list[MCPTool]:
        """List the tools the calling principal may see, in registration order.

        Visibility is scoped by the *same* checks `tools/call` performs, in the
        same order: declared scopes first, then what this connection hid, then
        a configured filter. None of the three can reveal a tool an earlier
        one rejected, and all three are enforced identically at call time - a
        tool this method omits is also a tool `tools/call` refuses, as
        "Unknown tool", not a permission error.

        The spec permits exactly this axis - the tool set "MAY vary by the
        authorization presented on the request [...] since credentials are
        per-request input, not connection state" - so it is evaluated per request
        and never memoized.
        """
        unnarrowed = self._unnarrowed_tools()
        if unnarrowed is not None:
            return list(unnarrowed.values())
        principal = current_principal()
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        # Read once rather than once per tool: it cannot change inside this pass.
        if principal is None:
            scoped = [
                tool
                for tool in self.registry.tools.values()
                if not tool.required_scopes and not (hidden and tool.name in hidden)
            ]
        else:
            holds = principal.has_scopes
            scoped = [
                tool
                for tool in self.registry.tools.values()
                if (not tool.required_scopes or holds(tool.required_scopes))
                and not (hidden and tool.name in hidden)
            ]
        tool_filter = self._tool_filter
        if tool_filter is None:
            return scoped
        return await _tools_passing_filter(tool_filter, scoped, principal)

    @staticmethod
    def _describe_tool(tool: MCPTool) -> dict[str, Any]:
        """Return this tool's `tools/list` entry for the era being answered.

        The era is read here rather than chosen by the caller, so no site that
        advertises a tool definition can advertise the wrong shape by forgetting
        to ask. Callers treat the returned mapping as read-only.
        """
        return _tool_describer(_era_modern_var.get())(tool)
