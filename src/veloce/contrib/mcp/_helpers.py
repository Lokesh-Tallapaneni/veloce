"""Pure MCP server helpers — shaping functions, wire constants and per-call state.

These touch no `MCPServer` instance state: they shape tool / resource / prompt
values into their MCP wire forms, map HTTP semantics onto MCP annotation hints,
and carry the markers the invocation path threads back to the dispatcher.

The module also owns the `META_*` keys the modern revision reserves on the wire,
the dispatch core's per-call ContextVars and the mutable `_InFlight` holder those
vars carry. Being a leaf keeps them reachable from `session` and the transports
without importing the dispatch core.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import orjson

from veloce._internal import is_json_mimetype
from veloce._model_backend import shape_through_model
from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_QUERY,
    HTTP_METHOD_TRACE,
)
from veloce.contrib.mcp.content import (
    AudioContent,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)
from veloce.contrib.mcp.errors import _InBandError, _InvalidArgumentsError
from veloce.contrib.mcp.icons import render_icons
from veloce.encoders import orjson_default
from veloce.http.response import Response
from veloce.principal import current_principal
from veloce.routing.converters import path_param_converters

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.context import MCPContext
    from veloce.contrib.mcp.prompts import MCPPrompt
    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.resources import MCPResource


# Prefixed per the spec, so an application's own `_meta` cannot collide.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
# Per request, not per connection; omitting it means no notifications at all.
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"


# ── Per-call state ──────────────────────────────────────────

# ContextVars, because concurrent HTTP calls share one `MCPServer`.

# The current call's outbound notification sink; `None` means no channel is wired.
_notifier_var: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar(
    "_mcp_notifier", default=None
)


# The call's `logging/setLevel` minimum; `None` until set, meaning emit all.
_log_level_var: ContextVar[str | None] = ContextVar("_mcp_log_level", default=None)

# Set for an id-bearing request so the invocation can attach its `MCPContext`.
_inflight_var: ContextVar[_InFlight | None] = ContextVar("_mcp_inflight", default=None)


# The spec defines a `subscriptions/listen` stream's subscription id as this id.
_request_id_var: ContextVar[Any] = ContextVar("_mcp_request_id", default=None)

# Per message, not per connection: the revision negotiates nothing and states its
# version on every request, so one connection may carry both eras.
_era_modern_var: ContextVar[bool] = ContextVar("_mcp_era_modern", default=False)

# Set by a bidirectional transport; `None` elsewhere, where `sample` raises.
_requester_var: ContextVar[Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None] = (
    ContextVar("_mcp_requester", default=None)
)


# ── Result shaping ──────────────────────────────────────────


def _attach_result_meta(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Attach what the handler asked to send back on this call's result.

    An existing `_meta` the result already carries wins: the protocol machinery
    that put it there (a task marker, a subscription id) is answering the client's
    own request, and a handler must not be able to displace it.
    """
    result["_meta"] = {**meta, **result.get("_meta", {})}
    return result


class _InFlight:
    """One id-bearing request the client may cancel while it runs.

    `cancel` flips the context flag, so a handler polling `ctx.cancelled` stops,
    and cancels the task, so one blocked on an `await` unwinds. The `initialize`
    request is never registered: the spec forbids cancelling it.
    """

    __slots__ = ("task", "context")

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        # `None` for a method that builds no `MCPContext` to expose.
        self.context: MCPContext | None = None

    def cancel(self) -> None:
        """Mark the call cancelled and unwind its task."""
        if self.context is not None:
            self.context._mark_cancelled()
        self.task.cancel()


# HTTP-method semantics as MCP annotation hints, which a client may ignore.
_READONLY_METHODS = frozenset(
    {HTTP_METHOD_GET, HTTP_METHOD_HEAD, HTTP_METHOD_OPTIONS, HTTP_METHOD_TRACE, HTTP_METHOD_QUERY}
)
_IDEMPOTENT_METHODS = frozenset(
    {
        HTTP_METHOD_GET,
        HTTP_METHOD_HEAD,
        HTTP_METHOD_PUT,
        HTTP_METHOD_DELETE,
        HTTP_METHOD_OPTIONS,
        HTTP_METHOD_TRACE,
        HTTP_METHOD_QUERY,
    }
)
_NON_DESTRUCTIVE_METHODS = frozenset(
    {
        HTTP_METHOD_GET,
        HTTP_METHOD_HEAD,
        HTTP_METHOD_POST,
        HTTP_METHOD_OPTIONS,
        HTTP_METHOD_TRACE,
        HTTP_METHOD_QUERY,
    }
)


def _tool_annotations(methods: list[str], title: str | None) -> dict[str, Any] | None:
    """Derive MCP tool annotation hints from a route's HTTP methods and title.

    A multi-verb route is rated conservatively across every verb it serves:
    read-only and idempotent only when *all* verbs qualify, destructive when
    *any* verb is non-additive. A pure `@app.mcp_tool` has no HTTP verb to map,
    so it gets a `title`-only annotation block when a title was set.
    """
    annotations: dict[str, Any] = {}
    if title:
        annotations["title"] = title
    if methods:
        verbs = {method.upper() for method in methods}
        read_only = verbs <= _READONLY_METHODS
        annotations["readOnlyHint"] = read_only
        annotations["idempotentHint"] = verbs <= _IDEMPOTENT_METHODS
        annotations["destructiveHint"] = not (verbs <= _NON_DESTRUCTIVE_METHODS)
        # A mutating route takes the spec's omitted open-world default instead.
        if read_only:
            annotations["openWorldHint"] = False
    return annotations or None


def _to_structured(value: Any) -> dict[str, Any] | None:
    """Render a tool result as the JSON object MCP `structuredContent` requires.

    A mapping passes through; a Pydantic model is dumped in JSON mode. A
    non-object result (list, scalar) has no object form, so `None` is returned
    and only the text content block is sent.
    """
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    # Through the adapter, so nested dates and enums match the declared schema.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        dumped = shape_through_model(value, type(value))
        if isinstance(dumped, dict):
            return dumped
    return None


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build a tool-call result whose content is a single text block.

    An in-band failure (a timeout, a raised handler, a non-conforming result)
    sets `isError`; a plain text success omits it.
    """
    result: dict[str, Any] = {"content": [TextContent(text).to_payload()]}
    if is_error:
        result["isError"] = True
    return result


def _content_blocks(value: Any) -> list[dict[str, Any]] | None:
    """Render a handler's content-block return as a result's content array.

    A tool result carries a *list* of content blocks, emitted in order; a single
    block is accepted as itself. Any other return is data, so this costs one
    `isinstance` on the common path, and a list is probed by its first element
    only rather than scanned.
    """
    if isinstance(value, ContentBlock):
        return [value.to_payload()]
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], ContentBlock):
        blocks = []
        for index, item in enumerate(value):
            if not isinstance(item, ContentBlock):
                # Rather than emit an object repr as if it were the answer.
                raise _InBandError(
                    "a tool returning content blocks must return only content "
                    f"blocks; item {index} is {type(item).__name__}"
                )
            blocks.append(item.to_payload())
        return blocks
    return None


def _binary_result(response: Response) -> dict[str, Any] | None:
    """Shape an image/audio response body into a typed MCP content block.

    MCP defines first-class `image` and `audio` content blocks carrying the bytes
    as base64 with their media type; any other media type returns `None` and the
    caller shapes it as text or structured content. A 4xx/5xx still flags
    `isError`, so an error is never read as a successful result.
    """
    mimetype = response.mimetype
    if mimetype.startswith("image/"):
        block: ContentBlock = ImageContent(
            base64.b64encode(response.body or b"").decode("ascii"), mimetype
        )
    elif mimetype.startswith("audio/"):
        block = AudioContent(base64.b64encode(response.body or b"").decode("ascii"), mimetype)
    else:
        return None
    result: dict[str, Any] = {"content": [block.to_payload()]}
    if response.status_code >= 400:
        result["isError"] = True
    return result


# Opt-in headers carrying an MCP resource URI, harmless on the HTTP door.
_HEADER_RESOURCE_LINK = "x-mcp-resource-link"
_HEADER_EMBEDDED_RESOURCE = "x-mcp-embedded-resource"


def _mcp_resource_header(response: Response, name: str) -> str | None:
    """Return a response header value by case-insensitive name, or `None`.

    `Response.headers` is keyed by the casing the handler chose, so a fixed-case
    lookup would miss `X-MCP-Resource-Link` set as `x-mcp-...`. The scan runs
    only on the MCP tool-call path, never the HTTP hot path.
    """
    for key, value in response.headers.items():
        if key.lower() == name:
            return value
    return None


def _resource_result_from_response(tool: MCPTool, response: Response) -> dict[str, Any] | None:
    """Emit a resource-link / embedded-resource result when the response asks for one.

    A route signals the intent with the `X-MCP-Resource-Link` /
    `X-MCP-Embedded-Resource` header carrying the resource URI: the link form
    references it for a later `resources/read`, the embedded form inlines the
    body's contents. Neither header returns `None`.
    """
    link_uri = _mcp_resource_header(response, _HEADER_RESOURCE_LINK)
    if link_uri:
        block: ContentBlock = ResourceLink(
            link_uri, tool.name, title=tool.title, description=tool.description
        )
        return {"content": [block.to_payload()]}
    embed_uri = _mcp_resource_header(response, _HEADER_EMBEDDED_RESOURCE)
    if embed_uri:
        block = EmbeddedResource(_resource_contents(embed_uri, response))
        return {"content": [block.to_payload()]}
    return None


def _build_resource_listing_entry(resource: MCPResource, uri_key: str) -> dict[str, Any]:
    """Shape a resource into its list entry under the given URI key."""
    entry: dict[str, Any] = {
        uri_key: resource.uri,
        "name": resource.name,
        "description": resource.description,
    }
    if resource.title:
        entry["title"] = resource.title
    mime_type = _declared_mime_type(resource)
    if mime_type:
        entry["mimeType"] = mime_type
    icons = render_icons(resource.icons)
    if icons is not None:
        entry["icons"] = icons
    if resource.size is not None:
        entry["size"] = resource.size
    if resource.annotations:
        entry["annotations"] = resource.annotations
    if resource.meta:
        entry["_meta"] = resource.meta
    return entry


def _declared_mime_type(resource: MCPResource) -> str | None:
    """Return the media type the route declares for a resource, if any.

    Only a declared type is advertised: an inferred one could contradict what
    `resources/read` goes on to return, which is worse than staying silent.
    """
    route_info = resource.tool.route_info
    if route_info is None:
        return None
    declared = getattr(route_info, "mcp_resource_mime_type", None)
    if isinstance(declared, str) and declared:
        return declared
    response_class = getattr(route_info, "response_class", None)
    media_type = getattr(response_class, "default_media_type", None)
    if not isinstance(media_type, str):
        return None
    # A read reports the bare media type, so the listing drops the parameters
    # RFC 9110 Sec. 8.3.1 allows rather than advertising a type the read will not.
    return media_type.split(";", 1)[0].strip() or None


def _describe_resource(resource: MCPResource) -> dict[str, Any]:
    """Return a static resource's `resources/list` entry, built once per resource."""
    entry = resource.listing_entry
    if entry is None:
        entry = resource.listing_entry = _build_resource_listing_entry(resource, "uri")
    return entry


def _describe_resource_template(resource: MCPResource) -> dict[str, Any]:
    """Return a template's `resources/templates/list` entry, built once per resource."""
    entry = resource.listing_entry
    if entry is None:
        entry = resource.listing_entry = _build_resource_listing_entry(resource, "uriTemplate")
    return entry


def _resource_contents(
    uri: str, response: Response, declared_mime_type: str | None = None
) -> dict[str, Any]:
    """Shape a resource route's response body into one MCP resource-contents entry.

    A JSON or `text/*` body is returned as `text`, any other media type as a
    base64 `blob`. The media type is the one the route declared when it named
    one, so a read reports what the listing advertised.
    """
    mimetype = declared_mime_type or response.mimetype
    body = response.body or b""
    entry: dict[str, Any] = {"uri": uri, "mimeType": mimetype}
    if is_json_mimetype(mimetype) or mimetype.startswith("text/"):
        entry["text"] = body.decode("utf-8", "replace")
    else:
        entry["blob"] = base64.b64encode(body).decode("ascii")
    return entry


def _build_prompt_listing_entry(prompt: MCPPrompt) -> dict[str, Any]:
    """Shape a prompt into its `prompts/list` entry."""
    entry: dict[str, Any] = {"name": prompt.name, "description": prompt.description}
    if prompt.title:
        entry["title"] = prompt.title
    icons = render_icons(prompt.icons)
    if icons is not None:
        entry["icons"] = icons
    if prompt.arguments:
        entry["arguments"] = prompt.arguments
    if prompt.meta:
        entry["_meta"] = prompt.meta
    return entry


def _describe_prompt(prompt: MCPPrompt) -> dict[str, Any]:
    """Return this prompt's `prompts/list` entry, built once per prompt."""
    entry = prompt.listing_entry
    if entry is None:
        entry = prompt.listing_entry = _build_prompt_listing_entry(prompt)
    return entry


def _user_text_message(text: str) -> dict[str, Any]:
    """Build a user-role MCP prompt message carrying a single text block."""
    return {"role": "user", "content": TextContent(text).to_payload()}


# An unrecognised role on a handler-built message falls back to "user".
_PROMPT_ROLES = frozenset({"user", "assistant"})


def _normalize_prompt_message(item: Any, dumps: Any = None) -> dict[str, Any]:
    """Normalise one prompt message item into an MCP prompt message.

    A string is a user text message; a mapping is read as a ``{"role", "content"}``
    message whose string content is wrapped into a text content block and whose
    unrecognised role falls back to user. Any other item is stringified.
    """
    if isinstance(item, str):
        return _user_text_message(item)
    if isinstance(item, dict):
        role = item.get("role")
        content = item.get("content")
        if isinstance(content, str):
            content = TextContent(content).to_payload()
        return {"role": role if role in _PROMPT_ROLES else "user", "content": content}
    return _user_text_message(_stringify(item, dumps))


def _normalize_prompt_messages(result: Any, dumps: Any = None) -> list[dict[str, Any]]:
    """Normalise a prompt callable's return into MCP prompt messages.

    A string becomes a single user text message; a list becomes one message per
    item; any other return is stringified into a single user message.
    """
    if isinstance(result, str):
        return [_user_text_message(result)]
    if isinstance(result, list):
        return [_normalize_prompt_message(item, dumps) for item in result]
    return [_user_text_message(_stringify(result, dumps))]


# A `subscriptions/listen` response is the graceful close, so nothing is emitted.
_DEFERRED_RESPONSE = object()


def _principal_lacks_scopes(required: frozenset[str]) -> bool:
    """Return whether the current principal is missing any of `required`.

    A tool with no required scopes is always allowed; otherwise the request
    principal must hold every one, and an absent principal satisfies none.
    """
    if not required:
        return False
    principal = current_principal()
    return principal is None or not principal.has_scopes(required)


def _route_path_params(route_info: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build `request.path_params` for a route-backed tool call.

    A tool call has no URL, so the values the router would have matched arrive
    as named entries in the JSON `arguments`. Anything reading
    `request.path_params` then sees the mapping it would on the HTTP path.
    """
    params = {name: arguments[name] for name in route_info.param_names if name in arguments}
    # Through the converter the router would have applied, so a `{user_id:int}`
    # route cannot receive a string here. Refused in band, not as a non-match.
    for name, converter in path_param_converters(route_info.path_template).items():
        if name not in params:
            continue
        ok, converted = converter.match(str(params[name]))
        if not ok:
            raise _InvalidArgumentsError(f"{name}: invalid value for path parameter {name!r}")
        params[name] = converted
    for key, value in route_info.defaults.items():
        params.setdefault(key, value)
    return params


def _progress_token(params: dict[str, Any]) -> str | int | None:
    """Return the call's `_meta.progressToken`, or `None` when none was sent.

    A client opts into progress notifications by attaching a `progressToken` to a
    request's `_meta`; without one the server reports none.
    """
    meta = params.get("_meta")
    if isinstance(meta, dict):
        token = meta.get("progressToken")
        if isinstance(token, (str, int)) and not isinstance(token, bool):
            return token
    return None


def _response_body_value(response: Response) -> Any:
    """Unwrap a Response into the value `_stringify` should serialise.

    A JSON-typed body is decoded back to a Python value so the tool result
    carries the same JSON the HTTP client would receive; any other body decodes
    to its text. A streamed response was buffered by `_drain_stream` first.
    """
    body = response.body
    if not body:
        return ""
    if is_json_mimetype(response.mimetype):
        try:
            return orjson.loads(body)
        except orjson.JSONDecodeError:
            pass
    return body.decode("utf-8", "replace")


def _stringify(result: Any, dumps: Any = None) -> str:
    """Serialise a handler return value to the text content of a tool result.

    `dumps` is the application's serialiser, or `None` when it configured none
    and the direct encoder already emits the same bytes, so a `@app.mcp_tool` and
    a route-backed tool answer in one dialect. `MCPServer` resolves it once at
    construction, so this costs an argument rather than a per-call lookup: a
    `contextvar` read here measured 99ns against a 424ns render.
    """
    if isinstance(result, str):
        return result
    # A bare byte string is the result's text form; nested bytes are quoted.
    if isinstance(result, (bytes, bytearray, memoryview)):
        return _bytes_to_text(result)
    # No fallback to `str(result)`: a repr of a `Secret` or a cycle would put
    # exactly what this encoder prevents into the model's context.
    if dumps is not None:
        encoded: bytes = dumps(result)
        return encoded.decode()
    return orjson.dumps(result, default=_orjson_default).decode()


def encode_envelope(payload: Any) -> bytes:
    """Encode a JSON-RPC envelope, or any other MCP protocol document.

    Protocol frames are not the application's to restyle, the rule `json_provider`
    states for the framework's own wire formats. The application's dialect still
    reaches a tool result's content through `_stringify`; this is the envelope
    around it.
    """
    return orjson.dumps(payload, default=_orjson_default)


def _orjson_default(value: Any) -> Any:
    """Fallback serialiser for values orjson cannot encode natively.

    Delegates to the framework's own encoder, so one handler answering both doors
    produces the same JSON either way, down to a `Secret` being refused. Bytes
    are the one deliberate difference - see `_bytes_to_text`.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes_to_text(value)
    return orjson_default(value)


def _bytes_to_text(value: bytes | bytearray | memoryview) -> str:
    """Render a raw byte string as tool-result text.

    Decoded UTF-8 is what a caller returning `b"..."` means; bytes that are not
    UTF-8 are base64-encoded rather than decoded with replacement, which would
    corrupt them irrecoverably. The same choice a resource body makes between
    `text` and a `blob`, on the bytes rather than on a media type.
    """
    raw = bytes(value)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii")


class _ShortCircuit:
    """A `before_request` hook's `Response`, returned in place of the handler call."""

    __slots__ = ("response",)

    def __init__(self, response: Response) -> None:
        self.response = response


class _RouteResponse:
    """A route-backed tool's final `Response`, returned instead of the raw value.

    Shaped and `after_request`-rewritten, or built by an exception handler.

    `model_filtered` records whether the route's `response_model` filter ran over
    the value this response carries, which decides whether the decoded body may
    be advertised as schema-conformant `structuredContent`.
    """

    __slots__ = ("response", "model_filtered")

    def __init__(self, response: Response, model_filtered: bool = False) -> None:
        self.response = response
        self.model_filtered = model_filtered


def transport_route_name(base: str, path: str) -> str:
    """Build a route name unique to one mount of one transport.

    `mount_mcp` supports mounting at two paths, so a name taken from the handler
    function alone would leave the first mount unreachable by name. These routes
    carry `include_in_schema=False`, so qualifying the name by path costs nothing.
    """
    return f"{base}:{path}"
