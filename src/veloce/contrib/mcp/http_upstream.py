"""HTTP upstream helper — a session-aware `request()` for `add_mcp_proxy`.

Speaks the real Streamable HTTP handshake against an upstream MCP server:
`initialize`, the `Mcp-Session-Id` response header, `notifications/initialized`,
and carrying that session id on every call after. Clean-room, built on
`httpx2` only - no dependency on `mcp` or `fastmcp`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import veloce._json as _json
from veloce.contrib.mcp.errors import MCPError
from veloce.contrib.mcp.server import LATEST_PROTOCOL_VERSION, is_modern_version

if TYPE_CHECKING:  # pragma: no cover
    import httpx2


class _UpstreamError(MCPError):
    """An upstream's JSON-RPC error, carrying its own code rather than a fixed one."""

    def __init__(self, message: str, code: int, *, data: Any = None) -> None:
        super().__init__(message, data=data)
        self.code = code


class _SessionExpired(Exception):
    """Internal signal: a 404 arrived for a request that carried a session id.

    Never escapes `HttpUpstreamRequest.__call__` - caught one frame up, which
    decides whether to re-handshake.
    """

    def __init__(self, stale_session_id: str) -> None:
        self.stale_session_id = stale_session_id


class HttpUpstreamRequest:
    """A `request()` callable for `add_mcp_proxy`, bound to one upstream URL.

    Callable as `add_mcp_proxy` expects (`await request(method, params)`) and
    also exposes `await request.aclose()` for app-shutdown cleanup.
    """

    def __init__(
        self,
        url: str,
        client: httpx2.AsyncClient,
        owns_client: bool,
        headers: Mapping[str, str] | None,
    ) -> None:
        self._url = url
        self._client = client
        self._owns_client = owns_client
        self._headers = dict(headers or {})
        self._session_id: str | None = None
        self._protocol_version: str = LATEST_PROTOCOL_VERSION
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._next_id = itertools.count(1)

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            async with self._init_lock:
                if not self._initialized:
                    await self._handshake()
        try:
            return await self._attempt(method, params, retry_signal=True)
        except _SessionExpired as exc:
            async with self._init_lock:
                # A concurrent caller may have already fixed the session
                # while this one waited for the lock - only re-handshake if
                # the id that just 404'd is still the one currently held.
                if self._session_id == exc.stale_session_id:
                    self._session_id = None
                    self._initialized = False
                    await self._handshake()
            return await self._attempt(method, params, retry_signal=False)

    def _build_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        headers["accept"] = "application/json, text/event-stream"
        headers["content-type"] = "application/json"
        headers["mcp-protocol-version"] = self._protocol_version
        if self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _attempt(
        self,
        method: str,
        params: dict[str, Any],
        *,
        retry_signal: bool,
        expect_body: bool = True,
    ) -> dict[str, Any]:
        """One POST + dispatch.

        `retry_signal` controls whether a 404 with a sent session id raises
        `_SessionExpired` (`True`) or falls through to `raise_for_status()`
        (`False` - the one retry already happened). `expect_body=False` is
        for `notifications/initialized` only: no id, no response to parse.
        """
        headers = self._build_headers()
        sent_session_id = headers.get("mcp-session-id")
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if expect_body:
            envelope["id"] = next(self._next_id)
        request_body = _json.dumps(envelope)

        async with self._client.stream(
            "POST", self._url, content=request_body, headers=headers
        ) as response:
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id

            if response.status_code == 404 and retry_signal and sent_session_id is not None:
                raise _SessionExpired(sent_session_id)

            if not response.is_success:
                error_body = await response.aread()
                parsed = _try_parse_json_rpc_error(error_body)
                if parsed is not None:
                    code, message, data = parsed
                    raise _UpstreamError(message, code, data=data)
                response.raise_for_status()

            if not expect_body:
                return {}

            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            result: dict[str, Any]
            if content_type == "text/event-stream":
                result = await _read_sse_envelope(response)
            else:
                response_body = await response.aread()
                result = _json.loads(response_body)

        if "error" in result:
            err = result["error"]
            raise _UpstreamError(
                err.get("message", "upstream error"), err.get("code", -32603), data=err.get("data")
            )
        upstream_result: dict[str, Any] = result["result"]
        return upstream_result

    async def _handshake(self) -> None:
        envelope = await self._attempt(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "veloce", "version": _veloce_version()},
            },
            retry_signal=False,  # no session yet to expire on the very first call
        )
        negotiated = envelope.get("protocolVersion")
        if is_modern_version(negotiated):
            raise RuntimeError(
                f"upstream negotiated {negotiated!r}, the modern no-handshake "
                "MCP revision; http_upstream only speaks the handshake/session era."
            )
        self._protocol_version = negotiated or LATEST_PROTOCOL_VERSION
        await self._attempt("notifications/initialized", {}, retry_signal=False, expect_body=False)
        self._initialized = True

    async def aclose(self) -> None:
        if self._owns_client:
            if self._session_id is not None:
                with contextlib.suppress(Exception):
                    await self._client.delete(
                        self._url, headers={"Mcp-Session-Id": self._session_id}
                    )
            await self._client.aclose()


def _try_parse_json_rpc_error(body: bytes) -> tuple[int, str, Any] | None:
    try:
        parsed = _json.loads(body)
    except (_json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict) or "error" not in parsed:
        return None
    err = parsed["error"]
    if not isinstance(err, dict):
        return None
    return err.get("code", -32603), err.get("message", "upstream error"), err.get("data")


async def _read_sse_envelope(response: httpx2.Response) -> dict[str, Any]:
    """Read SSE `data:` lines, event by event, until one is a JSON-RPC result/error."""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
            continue
        if line == "" and data_lines:
            event: dict[str, Any] = _json.loads("".join(data_lines))
            data_lines = []
            if "result" in event or "error" in event:
                return event
    raise RuntimeError("upstream SSE stream ended without a JSON-RPC result or error event")


def _veloce_version() -> str:
    import veloce

    version: str = veloce.__version__
    return version


def http_upstream(
    url: str,
    *,
    client: httpx2.AsyncClient | None = None,
    headers: Mapping[str, str] | None = None,
) -> HttpUpstreamRequest:
    """Return a `request` callable for `add_mcp_proxy` that speaks Streamable HTTP.

    `client`, if given, is the caller's own `httpx2.AsyncClient` - the caller
    keeps owning its connection pool, auth, and retries. When omitted, this
    creates and owns one; `await request.aclose()` closes it and best-effort
    ends the upstream session.

    `headers` are merged onto every request. Where a name collides with a
    protocol-required header (`Accept`, `Content-Type`, `Mcp-Session-Id`,
    `MCP-Protocol-Version`), the protocol header wins.

    A protocol-level upstream failure raises `veloce.contrib.mcp.MCPError` -
    catch that, not a private subclass. An in-band tool failure
    (`result.isError: true`) is returned as data, never raised.

    Usage::

        from veloce.contrib.mcp import add_mcp_proxy, http_upstream

        request = http_upstream("http://localhost:9000/mcp")
        await add_mcp_proxy(app, "upstream", request)
        app.mount_mcp(transport="http")
    """
    try:
        import httpx2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "http_upstream needs the 'httpx2' package. "
            "Install it with: pip install veloceframework[mcp-proxy]"
        ) from exc
    owns_client = client is None
    resolved_client = (
        client
        if client is not None
        else httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=120.0))
    )
    return HttpUpstreamRequest(url, resolved_client, owns_client, headers)
