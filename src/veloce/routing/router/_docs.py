"""Shared `Doc` strings for the route-declaring signatures."""

from __future__ import annotations

from typing_extensions import Doc

# Shared by `add_route` and `route` rather than copied, so a change reaches both
# signatures at once and neither tooltip can drift from the other.
_DOC_MCP_RESOURCE_URI = Doc(
    "Resource URI for the route's MCP resource: a static URI, or a URI "
    "template (`users://{user_id}`) binding its path parameters."
)

_DOC_MCP_RESOURCE_MIME_TYPE = Doc(
    "Media type advertised for the route's MCP resource. Declared rather "
    "than inferred, so the listing never disagrees with what a read returns."
)

_DOC_MCP_META = Doc(
    "`_meta` published on this route's MCP tool or resource, for metadata "
    "an extension defines rather than the protocol itself."
)

_DOC_MCP_TASK_SUPPORT = Doc(
    "Allow this route's MCP tool to run as a background task "
    "(task-augmented `tools/call`, polled via `tasks/get` / `tasks/result`)."
)

_DOC_MCP_ANNOTATIONS = Doc(
    "Tool annotation overrides (`readOnlyHint`, `idempotentHint`, "
    "`destructiveHint`, `openWorldHint`, `title`) for the route's MCP tool, "
    "merged over the hints its HTTP verb would otherwise imply."
)

_DOC_RESPONSE_MODEL = Doc(
    "Type used to filter and serialize the handler's return value and the OpenAPI "
    "response schema. Defaults to the handler's return annotation when it names a "
    "model; pass `None` to declare no response contract."
)

_DOC_STREAM = Doc(
    "Opt into request-body streaming: the body is not buffered before the "
    "handler, so the handler may consume `request.stream()` incrementally. "
    "The synchronous body accessors are unavailable on a streaming route "
    "until the body is drained."
)

_DOC_EXCLUDE_MIDDLEWARE = Doc(
    "Middleware this route opts out of, as classes or as resolved names. A "
    "class matches by type, so it covers subclasses and cannot be misspelled; "
    "a string matches the middleware's resolved `name` exactly, which is how "
    "two instances of one class are told apart."
)
