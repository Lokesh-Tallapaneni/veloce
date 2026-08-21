"""Pure MCP server helpers — module-level shaping functions and marker classes.

These touch no `MCPServer` instance state: they shape tool / resource / prompt
values into their MCP wire forms, map HTTP semantics onto MCP annotation hints,
and carry the small marker objects the invocation path threads back to the
dispatcher. `server.py` and the `TasksMixin` / `InvocationMixin` mixins import
what they need from here, keeping the stateful dispatch core focused.
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
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.errors import _InBandError
from veloce.contrib.mcp.icons import render_icons
from veloce.http.response import Response
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import MCPPrompt
    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.resources import MCPResource
    from veloce.contrib.mcp.session import MCPSession


# The current call's outbound notification sink, scoped per request so a handler's
# progress / log notifications reach the right client. A ContextVar (not an
# instance attribute) keeps concurrent calls isolated on the Streamable HTTP
# transport, where many requests share one `MCPServer`; the serial stdio transport
# sets it once per serve task. `None` means no channel is wired (off-transport).
_notifier_var: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar(
    "_mcp_notifier", default=None
)

# The current call's `logging/setLevel` minimum, scoped per request like the
# notifier so one HTTP client's level change cannot raise the floor for another.
# `None` until set (emit all). The serial stdio loop sets it once in its serve
# task, where it persists for the connection.
_log_level_var: ContextVar[str | None] = ContextVar("_mcp_log_level", default=None)

# The current request's in-flight registration, set by `handle_message` for an
# id-bearing (cancellable) request so the invocation can attach its `MCPContext`.
# `None` for a notification or off-dispatch construction. A ContextVar keeps
# concurrent HTTP calls isolated, exactly like the notifier / log-level vars.
_inflight_var: ContextVar[_InFlight | None] = ContextVar("_mcp_inflight", default=None)

# The dispatching connection's session, set by `handle_message` when a stateful
# transport passes one, so a per-connection method (`resources/subscribe`) reads
# the session it should mutate without the handler signature gaining a parameter.
# `None` on the stateless path. A ContextVar isolates concurrent connections.
_session_var: ContextVar[MCPSession | None] = ContextVar("_mcp_session", default=None)

# The JSON-RPC id of the request being dispatched. A `subscriptions/listen` needs
# it because the spec defines the stream's subscription id as that id, and the
# handler signature carries only params. Set once per message beside the session.
_request_id_var: ContextVar[Any] = ContextVar("_mcp_request_id", default=None)

# The current call's server->client request issuer, set by a bidirectional
# transport so a tool's `MCPContext.sample` / `elicit` / `roots` can call the
# client and await the correlated reply. `None` off a bidirectional transport (the
# one-way HTTP/stdio default), where those methods raise. A ContextVar keeps
# concurrent connections' requesters isolated, like the notifier var.
_requester_var: ContextVar[Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None] = (
    ContextVar("_mcp_requester", default=None)
)

# Set on a detached task runner (`_run_task`) so the serial stdio transport can
# refuse a server->client request issued from it. A task-augmented call returns
# its `CreateTaskResult` immediately, so the stdio serve loop resumes reading
# stdin while the runner executes; if the runner called `ctx.sample` / `elicit` /
# `roots` its `request()` would start a second reader of the same stdin, racing
# the serve loop for inbound lines. `False` on the synchronous call path, where
# the serve loop is parked in the handler and `request()` is the sole reader.


class _InFlight:
    """One id-bearing request the client may cancel while it runs.

    Holds the request's task and - once `_invoke` builds it - the call's
    `MCPContext`. `cancel` flips the context flag (so a cooperative handler that
    polls `ctx.cancelled` stops) and cancels the task (so a handler blocked on an
    `await` unwinds). The `initialize` request is never registered: the spec
    forbids cancelling it.
    """

    __slots__ = ("task", "context")

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        # Attached by `_invoke` when the tool context exists; `None` for a method
        # (resources/read, prompts/get) that builds no `MCPContext` to expose.
        self.context: MCPContext | None = None

    def cancel(self) -> None:
        """Mark the call cancelled and unwind its task."""
        if self.context is not None:
            self.context._mark_cancelled()
        self.task.cancel()


# HTTP-method semantics mapped to MCP tool annotation hints. Read-only verbs do
# not modify state; idempotent verbs are safe to retry; a mutating verb that is
# not purely additive (PUT/PATCH/DELETE) is flagged destructive so a client can
# prompt for consent. These are advisory hints a client may ignore.
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
    *any* verb is non-additive (so a `GET`+`DELETE` route is flagged
    destructive, not read-only). `openWorldHint` is `False` only for a fully
    read-only route - an operation over the server's own data is closed-world;
    any mutating route is left to the spec's open-world default (omitted). The
    human-facing `title`, when the route declares a summary, is carried too. A
    pure `@app.mcp_tool` (no route) has no HTTP verb to map; it still gets a
    `title`-only annotation block when an explicit title was set.
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
        # A read-only route operates only on the server's own resources, so it is
        # a closed-world operation; a mutating route may reach external systems,
        # which the spec's omitted (open-world) default already covers.
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
    # A dataclass instance carries the same object form its declared schema
    # describes; dump it through the adapter so nested dates and enums render
    # the way the schema says they will.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        dumped = shape_through_model(value, type(value))
        if isinstance(dumped, dict):
            return dumped
    return None


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build a tool-call result whose content is a single text block.

    Every successful-text and in-band-error tool result shares this single
    text-content shape; routing them through `TextContent` keeps the wire form in
    one place. An in-band failure (a timeout, a raised handler, a non-conforming
    result) sets `isError`; a plain text success omits it.
    """
    result: dict[str, Any] = {"content": [TextContent(text).to_payload()]}
    if is_error:
        result["isError"] = True
    return result


def _content_blocks(value: Any) -> list[dict[str, Any]] | None:
    """Render a handler's content-block return as a result's content array.

    A tool result carries a *list* of content blocks, so a handler that has more
    than one thing to say - a caption beside an image, a chart beside the figures
    behind it - returns the blocks it wants and they are emitted in order. A
    single block is accepted as itself, since wrapping one block in a list to say
    "just this" is noise.

    Any other return is data and shapes as before, so this costs one `isinstance`
    on the common path. A list is probed by its first element only: a
    `list[int]` return must not pay a scan of every item to learn it is not
    blocks.
    """
    if isinstance(value, ContentBlock):
        return [value.to_payload()]
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], ContentBlock):
        blocks = []
        for index, item in enumerate(value):
            if not isinstance(item, ContentBlock):
                # Silently serialising the rest would emit an object repr as if it
                # were the answer; name the offending position instead.
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
    as base64 with their media type. A body of either kind has no useful text
    form, so it is emitted as that typed block instead of a garbled decoded-text
    block; any other media type returns `None` and the caller shapes it as text /
    structured content as before. A 4xx/5xx still flags `isError` so an error is
    never read as a successful result.
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


# Response headers a route handler sets to deliver its MCP result as a resource
# reference rather than inline content. Both are opt-in: a response without them
# takes the unchanged text/structured/binary path, so the wire form is identical
# for a route that uses neither. The link form references a resource the client
# reads later (`resources/read`); the embedded form inlines the body's contents
# so the agent reads the data without a follow-up call. The values are an MCP
# resource URI; they are harmless custom headers on the HTTP door.
_HEADER_RESOURCE_LINK = "x-mcp-resource-link"
_HEADER_EMBEDDED_RESOURCE = "x-mcp-embedded-resource"


def _mcp_resource_header(response: Response, name: str) -> str | None:
    """Return a response header value by case-insensitive name, or `None`.

    `Response.headers` is a plain dict keyed by the casing the handler chose, so
    a fixed-case lookup would miss `X-MCP-Resource-Link` set as `x-mcp-...`. The
    scan runs only on the MCP tool-call path (never the HTTP hot path) and over a
    handful of headers, so the per-call cost is immaterial.
    """
    for key, value in response.headers.items():
        if key.lower() == name:
            return value
    return None


def _resource_result_from_response(tool: MCPTool, response: Response) -> dict[str, Any] | None:
    """Emit a resource-link / embedded-resource result when the response asks for one.

    A route signals the intent with the `X-MCP-Resource-Link` /
    `X-MCP-Embedded-Resource` header carrying the resource URI. The link form
    references the URI (the client follows it with `resources/read`); the
    embedded form inlines the body's contents at that URI. A response carrying
    neither header returns `None`, leaving the caller's existing shaping path
    untouched.
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

    Only a declared type is advertised. The response class is picked from the
    handler's actual return value, so a type inferred from its annotation could
    disagree with what `resources/read` goes on to return - and a listing that
    contradicts the read is worse than one that stays silent.
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
    # A read reports the bare media type, so the listing drops any parameters
    # (RFC 9110 Sec. 8.3.1) rather than advertising `text/html; charset=utf-8`
    # against a read that answers `text/html`.
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

    A JSON or `text/*` body is returned as `text` (the value an agent reads); any
    other media type is returned as a base64 `blob`. The entry carries the read
    URI and the resource's media type: the one the route declared when it named
    one, so a read reports what the listing advertised, and the response's own
    otherwise.
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


# Valid MCP prompt message roles; an unrecognised role from a handler-built
# message falls back to "user".
_PROMPT_ROLES = frozenset({"user", "assistant"})


def _normalize_prompt_message(item: Any) -> dict[str, Any]:
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
    return _user_text_message(_stringify(item))


def _normalize_prompt_messages(result: Any) -> list[dict[str, Any]]:
    """Normalise a prompt callable's return into MCP prompt messages.

    A string becomes a single user text message; a list becomes one message per
    item; any other return is stringified into a single user message.
    """
    if isinstance(result, str):
        return [_user_text_message(result)]
    if isinstance(result, list):
        return [_normalize_prompt_message(item) for item in result]
    return [_user_text_message(_stringify(result))]


# Returned by a handler whose request is answered later rather than now. A
# `subscriptions/listen` is a long-lived request: its response is the graceful
# close, so the dispatcher must emit nothing when the handler returns.
_DEFERRED_RESPONSE = object()


def _principal_lacks_scopes(required: frozenset[str]) -> bool:
    """Return whether the current principal is missing any of `required`.

    A tool / resource with no required scopes is always allowed. Otherwise the
    request principal (set by the transport's authentication) must hold every
    required scope; an absent principal can satisfy no non-empty requirement.
    """
    if not required:
        return False
    principal = current_principal()
    return principal is None or not principal.has_scopes(required)


def _route_path_params(route_info: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build `request.path_params` for a route-backed tool call.

    The HTTP path fills `path_params` from the URL segments the router matched;
    a tool call has no URL, so the equivalent values arrive as named entries in
    the JSON `arguments`. Copy each argument whose name is one of the route's
    declared path parameters, then fill any route `defaults` not supplied, so a
    hook / dependency / handler that reads `request.path_params` sees the same
    mapping it would on the HTTP path.
    """
    params = {name: arguments[name] for name in route_info.param_names if name in arguments}
    for key, value in route_info.defaults.items():
        params.setdefault(key, value)
    return params


def _progress_token(params: dict[str, Any]) -> str | int | None:
    """Return the call's `_meta.progressToken`, or `None` when none was sent.

    A client opts into progress notifications by attaching a `progressToken` to a
    request's `_meta`; without one the server reports no progress (per the MCP
    progress utility).
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
    to its text. The body bytes are the already-rendered response body, so no
    further response-model work is needed. A streamed response has been buffered
    into its body by `_drain_stream` before this runs.
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


def _stringify(result: Any) -> str:
    """Serialise a handler return value to the text content of a tool result."""
    if isinstance(result, str):
        return result
    # A bare byte string is the text form of the result, not a value inside it,
    # so it is handed over as text rather than JSON-encoded into a quoted
    # string. Bytes nested in a mapping or sequence go through the encoder's
    # fallback below and are quoted like any other member.
    if isinstance(result, (bytes, bytearray, memoryview)):
        return _bytes_to_text(result)
    try:
        return orjson.dumps(result, default=_orjson_default).decode()
    except (TypeError, orjson.JSONEncodeError):
        return str(result)


def _orjson_default(value: Any) -> Any:
    """Fallback serialiser for values orjson cannot encode natively."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes_to_text(value)
    return str(value)


def _bytes_to_text(value: bytes | bytearray | memoryview) -> str:
    """Render a raw byte string as tool-result text.

    A tool result carries text, so the bytes have to become a string. Decoded
    UTF-8 is what a caller returning `b"..."` means; bytes that are not UTF-8
    are binary and are base64-encoded rather than decoded with replacement,
    which would corrupt them irrecoverably. This mirrors how a resource body
    picks between `text` and a base64 `blob`, deciding on whether the bytes are
    text rather than on a media type a bare return value does not carry.
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
    """A route-backed tool's final `Response` (shaped + after_request-rewritten,
    or built by an exception handler), returned in place of the raw value.

    `model_filtered` records whether the route's `response_model` filter ran over
    the value this response carries: `True` when `_build_response` built it from
    a non-`Response` handler return, `False` when the handler returned its own
    `Response` (or an exception handler built it). The server uses it to decide
    whether the decoded body may be advertised as schema-conformant
    `structuredContent`.
    """

    __slots__ = ("response", "model_filtered")

    def __init__(self, response: Response, model_filtered: bool = False) -> None:
        self.response = response
        self.model_filtered = model_filtered
