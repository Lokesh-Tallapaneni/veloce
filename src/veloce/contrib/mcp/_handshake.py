"""Handshake and discovery — initialize, capability advertisement and cache hints."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp._helpers import (
    META_SERVER_INFO,
    _era_modern_var,
)
from veloce.contrib.mcp.icons import render_icons

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.capabilities import Capability
    from veloce.contrib.mcp.registry import ToolFilter
    from veloce.contrib.mcp.session import MCPSession
    from veloce.contrib.mcp.subscriptions import ConnectionRegistry
from veloce.contrib.mcp._protocol import (
    _CACHE_SCOPE_PRIVATE,
    _CACHE_SCOPE_PUBLIC,
    _LIST_METHODS,
    _SUPPORTED_PROTOCOL_VERSIONS,
    LATEST_PROTOCOL_VERSION,
    SERVED_PROTOCOL_VERSIONS,
)

_logger = logging.getLogger(__name__)


class HandshakeMixin:
    """Handshake and discovery behaviour of `MCPServer`, mixed in so the server module stays readable."""

    # `MCPServer` is slotted, so a mixin without this regains a `__dict__`.
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes and members the host `MCPServer` provides.
        server_name: Any
        server_version: Any
        server_title: Any
        server_icons: Any
        server_website_url: Any
        server_instructions: Any
        _capabilities: tuple[Capability, ...]
        _era_aware_capabilities: frozenset[Capability]
        _connections: ConnectionRegistry | None
        _cache_ttl_ms: int
        _tool_filter: ToolFilter | None
        _any_scoped_tools: bool
        _any_scoped_resources: bool
        _any_scoped_prompts: bool
        current_session: Callable[[], MCPSession | None]

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Echo a supported revision, else the server's latest for the client to judge.
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        capabilities = self._advertised_capabilities(_era_modern_var.get())
        server_info = self._server_info()
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": capabilities,
            "serverInfo": server_info,
        }
        if self.server_instructions:
            result["instructions"] = self.server_instructions
        return result

    def _advertised_capabilities(self, modern: bool = False) -> dict[str, Any]:
        """Collect every held capability's advertisement for the caller's revision.

        A `None` advertisement is dropped so a client never probes a primitive the
        app does not expose. The revision reaches each capability whose `advertise`
        accepts it, so one whose methods a revision retired withholds its entry
        rather than promising a method-not-found.
        """
        capabilities: dict[str, Any] = {}
        for capability in self._capabilities:
            entry = (
                capability.advertise(modern=modern)
                if capability in self._era_aware_capabilities
                else capability.advertise()
            )
            if entry is not None:
                capabilities.update(entry)
        return capabilities

    def _server_info(self) -> dict[str, Any]:
        """Identity block shared by `initialize` and `server/discover`.

        `title` is the human-facing display name; `name` / `version` carry the
        identifier and version.
        """
        info: dict[str, Any] = {"name": self.server_name, "version": self.server_version}
        if self.server_title:
            info["title"] = self.server_title
        icons = render_icons(self.server_icons)
        if icons is not None:
            info["icons"] = icons
        if self.server_website_url:
            info["websiteUrl"] = self.server_website_url
        return info

    async def _handle_discover(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `server/discover` with the versions, capabilities and identity.

        The modern revision requires this single probe in place of the `initialize`
        handshake. `supportedVersions` is ordered newest-first, so a client taking
        the head gets the newest revision both sides serve, and `serverInfo` travels
        in `_meta`, where that revision moved it.
        """
        capabilities = self._advertised_capabilities(_era_modern_var.get())
        extensions = self._advertised_extensions()
        if extensions:
            capabilities = {**capabilities, "extensions": extensions}
        return {
            "supportedVersions": list(SERVED_PROTOCOL_VERSIONS),
            "capabilities": capabilities,
            "_meta": {META_SERVER_INFO: self._server_info()},
            **({"instructions": self.server_instructions} if self.server_instructions else {}),
        }

    def _advertised_extensions(self) -> dict[str, Any]:
        """List the protocol extensions this server implements, for `server/discover`.

        A capability contributes an entry only when the feature it names is
        actually available, so a server with no task-capable tool advertises no
        tasks extension and a client will never offer one.
        """
        advertised: dict[str, Any] = {}
        for capability in self._capabilities:
            entry = capability.extensions()
            if entry:
                advertised.update(entry)
        return advertised

    def _add_cache_hints(
        self, method: str, result: dict[str, Any], session: MCPSession | None
    ) -> None:
        """Attach `ttlMs` / `cacheScope` to a cacheable `complete` result.

        A result that can differ between callers is `private`, so a shared gateway
        cannot serve one caller's answer to another; everything else is `public`.
        The spec is explicit that `cacheScope` is a hint and never a substitute for
        the per-primitive access control that already runs on each of these paths.
        """
        result["ttlMs"] = self._cache_ttl_ms
        result["cacheScope"] = (
            _CACHE_SCOPE_PRIVATE if self._varies_by_caller(method, session) else _CACHE_SCOPE_PUBLIC
        )

    def connection_is_stateful(self) -> bool:
        """Whether the connection being answered persists beyond this request.

        A stateless request has no outbound channel and no per-connection state, so
        nothing it is told survives the response.
        """
        session = self.current_session()
        return session is not None and session.persistent

    def connection_can_stream(self, session: MCPSession) -> bool:
        """Whether `session` holds an open outbound stream to push messages down.

        Weaker than `connection_is_stateful` and deliberately so: a stream that
        lives only for the request that opened it still delivers, which is the
        whole shape of a modern `subscriptions/listen`.
        """
        return self._connections is not None and self._connections.holds(session)

    def _varies_by_caller(self, method: str, session: MCPSession | None = None) -> bool:
        """Whether this method's result can differ between two authorized callers.

        `session` is passed in rather than read from the dispatch context: the
        hints are stamped after that context has been released.
        """
        if method == "resources/read":
            # Its route runs the full lifecycle under the caller's principal.
            return True
        if method == "server/discover":
            # Built per connection, so `public` would leak it across callers.
            return True
        if method not in _LIST_METHODS:
            return False
        # `MCPContext.hide` can narrow one connection's view at any point.
        if session is not None and session.persistent:
            return True
        if method == "tools/list":
            return self._tool_filter is not None or self._any_scoped_tools
        if method == "prompts/list":
            return self._any_scoped_prompts
        return self._any_scoped_resources
