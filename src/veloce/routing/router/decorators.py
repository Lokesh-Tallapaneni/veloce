"""Decorator API — `route`, the method shortcuts and the WebSocket forms."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any

from typing_extensions import Doc

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._internal import T_route
from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_QUERY,
    HTTP_METHOD_TRACE,
    ROUTE_METHOD_WEBSOCKET,
)
from veloce._ws_listener import build_listener_handler
from veloce.routing.route_info import (
    _INFER_RESPONSE_MODEL,
    RouteHandler,
    RouteOptions,
)
from veloce.routing.router._docs import (
    _DOC_EXCLUDE_MIDDLEWARE,
    _DOC_MCP_ANNOTATIONS,
    _DOC_MCP_META,
    _DOC_MCP_RESOURCE_MIME_TYPE,
    _DOC_MCP_RESOURCE_URI,
    _DOC_MCP_TASK_SUPPORT,
    _DOC_RESPONSE_MODEL,
    _DOC_STREAM,
)
from veloce.routing.router.registration import RegistrationMixin
from veloce.status import HTTP_200_OK


class DecoratorsMixin(RegistrationMixin):
    """The decorator API: `route`, the method shortcuts and the WebSocket forms."""

    def route(
        self,
        path: Annotated[
            str,
            Doc("URL path template, including `{param}` / `{param:converter}` placeholders."),
        ],
        methods: Annotated[
            list[str] | None,
            Doc("HTTP methods this handler serves; defaults to `GET`."),
        ] = None,
        *,
        dependencies: Annotated[
            list[Any] | None,
            Doc("Dependencies run for this route, appended after the router-level ones."),
        ] = None,
        response_model: Annotated[
            Any,
            _DOC_RESPONSE_MODEL,
        ] = _INFER_RESPONSE_MODEL,
        tags: Annotated[
            list[str] | None,
            Doc("OpenAPI tags for this route, combined with the router-level tags."),
        ] = None,
        summary: Annotated[
            str | None,
            Doc("Short OpenAPI summary for this operation."),
        ] = None,
        name: Annotated[
            str | None,
            Doc("Endpoint name for `url_for` reverse lookup; defaults to the handler's name."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("OpenAPI description; defaults to the handler's docstring."),
        ] = None,
        deprecated: Annotated[
            bool,
            Doc("Mark the operation as deprecated in the OpenAPI document."),
        ] = False,
        response_description: Annotated[
            str,
            Doc("Description of the successful response in the OpenAPI document."),
        ] = MSG_SUCCESSFUL_RESPONSE,
        status_code: Annotated[
            int,
            Doc("Default HTTP status code for a successful response."),
        ] = HTTP_200_OK,
        response_class: Annotated[
            Any,
            Doc("Response class for this route, overriding the router and framework defaults."),
        ] = None,
        response_model_include: Annotated[
            set[str] | None,
            Doc("Fields to include when serializing the response model."),
        ] = None,
        response_model_exclude: Annotated[
            set[str] | None,
            Doc("Fields to exclude when serializing the response model."),
        ] = None,
        response_model_exclude_unset: Annotated[
            bool,
            Doc("Omit fields left unset on the response model from the serialized output."),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                "Omit fields equal to their default on the response model from the serialized output."
            ),
        ] = False,
        response_model_by_alias: Annotated[
            bool,
            Doc("Serialize the response model using field aliases instead of attribute names."),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc("Omit fields whose value is `None` from the serialized response model."),
        ] = False,
        include_in_schema: Annotated[
            bool,
            Doc("Register the route but omit it from the generated OpenAPI document when False."),
        ] = True,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses for this route, overlaid on the router-level ones."),
        ] = None,
        operation_id: Annotated[
            str | None,
            Doc("Explicit OpenAPI `operationId`; defaults to the route name."),
        ] = None,
        openapi_extra: Annotated[
            dict[str, Any] | None,
            Doc("Arbitrary dict deep-merged into this route's OpenAPI operation object."),
        ] = None,
        defaults: Annotated[
            dict[str, Any] | None,
            Doc(
                "Fixed values merged into the path params at dispatch without overriding URL-matched ones."
            ),
        ] = None,
        callbacks: Annotated[
            dict[str, Any] | None,
            Doc(
                "OpenAPI Callback objects emitted verbatim into the operation's `callbacks` field."
            ),
        ] = None,
        strict_slashes: Annotated[
            bool | None,
            Doc(
                "When False, match both slashed and unslashed forms; `None` defers to the app policy."
            ),
        ] = None,
        subdomain: Annotated[
            str | None,
            Doc(
                "Constrain the route to a subdomain of `SERVER_NAME`; `*` matches any non-apex subdomain."
            ),
        ] = None,
        host: Annotated[
            str | None,
            Doc("Constrain the route to an exact `Host` header value (case-insensitive)."),
        ] = None,
        expose_as_mcp_tool: Annotated[
            bool,
            Doc("Expose the route as an MCP tool in the contrib MCP registry."),
        ] = False,
        mcp_description: Annotated[
            str | None,
            Doc("LLM-facing description for the route's MCP tool, required when exposed as one."),
        ] = None,
        expose_as_mcp_resource: Annotated[
            bool,
            Doc("Expose the read-only route as an MCP resource in the contrib MCP registry."),
        ] = False,
        mcp_resource_uri: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_URI,
        ] = None,
        mcp_resource_mime_type: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_MIME_TYPE,
        ] = None,
        mcp_meta: Annotated[
            dict[str, Any] | None,
            _DOC_MCP_META,
        ] = None,
        mcp_resource_size: Annotated[
            int | None,
            Doc("Size in bytes advertised for the route's MCP resource."),
        ] = None,
        mcp_resource_annotations: Annotated[
            dict[str, Any] | None,
            Doc("Annotations (audience, priority) advertised for the route's MCP resource."),
        ] = None,
        mcp_scopes: Annotated[
            Sequence[str] | None,
            Doc("Authorization scopes required to call this route over MCP."),
        ] = None,
        mcp_icons: Annotated[
            Sequence[Any] | None,
            Doc("Optional MCP `Icon` objects a client may render next to the tool/resource."),
        ] = None,
        mcp_task_support: Annotated[
            bool,
            _DOC_MCP_TASK_SUPPORT,
        ] = False,
        mcp_annotations: Annotated[
            dict[str, Any] | None,
            _DOC_MCP_ANNOTATIONS,
        ] = None,
        exclude_middleware: Annotated[
            Sequence[str | type[Any]] | None,
            _DOC_EXCLUDE_MIDDLEWARE,
        ] = None,
        stream: Annotated[
            bool,
            _DOC_STREAM,
        ] = False,
    ) -> Callable[[T_route], T_route]:
        """Register a route for any set of HTTP methods.

        `exclude_middleware=["CSRFMiddleware"]` opts this route out of the
        named middleware (matched against each middleware's `name`), so a
        webhook or health-check route can skip CSRF, auth, or rate limiting
        without forking the middleware. Routes that declare no exclusions
        pay no extra per-request cost.
        """
        options = RouteOptions(
            dependencies=dependencies,
            response_model=response_model,
            tags=tags,
            summary=summary,
            name=name,
            description=description,
            deprecated=deprecated,
            response_description=response_description,
            status_code=status_code,
            response_class=response_class,
            response_model_include=response_model_include,
            response_model_exclude=response_model_exclude,
            response_model_exclude_unset=response_model_exclude_unset,
            response_model_exclude_defaults=response_model_exclude_defaults,
            response_model_by_alias=response_model_by_alias,
            response_model_exclude_none=response_model_exclude_none,
            include_in_schema=include_in_schema,
            responses=responses,
            operation_id=operation_id,
            openapi_extra=openapi_extra,
            defaults=defaults,
            callbacks=callbacks,
            strict_slashes=strict_slashes,
            subdomain=subdomain,
            host=host,
            expose_as_mcp_tool=expose_as_mcp_tool,
            mcp_description=mcp_description,
            expose_as_mcp_resource=expose_as_mcp_resource,
            mcp_resource_uri=mcp_resource_uri,
            mcp_resource_mime_type=mcp_resource_mime_type,
            mcp_meta=mcp_meta,
            mcp_resource_size=mcp_resource_size,
            mcp_resource_annotations=mcp_resource_annotations,
            mcp_scopes=mcp_scopes,
            mcp_icons=mcp_icons,
            mcp_task_support=mcp_task_support,
            mcp_annotations=mcp_annotations,
            exclude_middleware=exclude_middleware,
            stream=stream,
        )

        def decorator(func: T_route) -> T_route:
            """Register `func` for the route and return it unchanged."""
            self._add_route_options(path, func, methods or [HTTP_METHOD_GET], options)
            return func

        return decorator

    def get(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`GET` route decorator. Safe and idempotent - RFC 9110 Sec. 9.3.1."""
        return self.route(path, methods=[HTTP_METHOD_GET], **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`POST` route decorator - RFC 9110 Sec. 9.3.3."""
        return self.route(path, methods=[HTTP_METHOD_POST], **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`PUT` route decorator. Idempotent - RFC 9110 Sec. 9.3.4."""
        return self.route(path, methods=[HTTP_METHOD_PUT], **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`PATCH` route decorator - RFC 5789."""
        return self.route(path, methods=[HTTP_METHOD_PATCH], **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`DELETE` route decorator. Idempotent - RFC 9110 Sec. 9.3.5."""
        return self.route(path, methods=[HTTP_METHOD_DELETE], **kwargs)

    def head(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`HEAD` route decorator. Like `GET` with no body - RFC 9110 Sec. 9.3.2."""
        return self.route(path, methods=[HTTP_METHOD_HEAD], **kwargs)

    def options(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`OPTIONS` route decorator - RFC 9110 Sec. 9.3.7."""
        return self.route(path, methods=[HTTP_METHOD_OPTIONS], **kwargs)

    def trace(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`TRACE` route decorator - RFC 9110 Sec. 9.3.8."""
        return self.route(path, methods=[HTTP_METHOD_TRACE], **kwargs)

    def query(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`QUERY` route decorator - RFC 10008.

        QUERY is safe and idempotent like GET but carries a request body like
        POST, for read-only operations whose parameters do not fit a URL (search,
        filtering, paging). The handler reads the body exactly as a POST handler
        does (`request.get_json()` / a body model parameter).
        """
        return self.route(path, methods=[HTTP_METHOD_QUERY], **kwargs)

    def websocket(
        self,
        path: Annotated[
            str,
            Doc("URL path template for the WebSocket route, including `{param}` placeholders."),
        ],
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Register a WebSocket route via decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(path=path, handler=func, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    def websocket_listener(
        self,
        path: str,
        *,
        receive: str = "json",
        send: str = "json",
        on_connect: RouteHandler | Callable[..., Any] | None = None,
        on_disconnect: RouteHandler | Callable[..., Any] | None = None,
    ) -> Callable[[RouteHandler | Callable[..., Any]], RouteHandler | Callable[..., Any]]:
        """Register a WebSocket route wrapping a per-message callback.

        The decorated callback handles one message at a time; the framework
        owns the accept handshake, the receive loop, and the clean close on
        disconnect. The callback is called as `cb(data)`, or `cb(ws, data)`
        when its first parameter is named `ws`/`socket` (or it takes two
        positional parameters). Returning a non-`None` value sends it back in
        `send` mode; returning `None` sends nothing.

        `receive`/`send` select the codec (`"json"` default, or `"text"` /
        `"bytes"`). `on_connect(ws)` runs after accept; `on_disconnect(ws)`
        always runs when the loop ends, including on peer disconnect. Sync
        callbacks and hooks are offloaded to the executor.

        Usage::

            @app.websocket_listener("/echo")
            async def echo(data):
                return data

        For full control over the handshake and loop use `@app.websocket`.
        """

        def decorator(func: RouteHandler | Callable[..., Any]) -> RouteHandler | Callable[..., Any]:
            """Build the listener handler, register it, and return `func`."""
            handler, _contract = build_listener_handler(
                func,
                receive=receive,
                send=send,
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
            self.add_route(path=path, handler=handler, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    def add_websocket_route(
        self,
        path: Annotated[
            str,
            Doc("URL path template for the WebSocket route, including `{param}` placeholders."),
        ],
        handler: Annotated[
            RouteHandler,
            Doc("Callable invoked with the accepted WebSocket connection when the route matches."),
        ],
    ) -> None:
        """Register a WebSocket route imperatively (ASGI shape).

        The non-decorator form of `@app.websocket(path)`.
        """
        self.add_route(path=path, handler=handler, methods=[ROUTE_METHOD_WEBSOCKET])

    def add_api_websocket_route(
        self, path: str, endpoint: RouteHandler, name: str | None = None
    ) -> None:
        """Register an imperative WebSocket route, mirroring `add_api_route`.

        The non-decorator form of `@app.websocket(path)`. `name`, when given,
        registers the route for reverse lookup so `app.url_for(name)` resolves
        to its path.
        """
        self.add_route(path=path, handler=endpoint, methods=[ROUTE_METHOD_WEBSOCKET], name=name)

    def add_api_route(
        self,
        path: str,
        endpoint: RouteHandler,
        *,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a route imperatively.

        The non-decorator form: the handler argument is named `endpoint`
        here and forwarded to `add_route` (where it is `handler`). All
        route kwargs - `response_model`, `tags`, `dependencies`,
        `status_code`, `openapi_extra`, ... - pass straight through.
        Defaults to `["GET"]` when `methods` is omitted.
        """
        self.add_route(
            path=path,
            handler=endpoint,
            methods=methods or [HTTP_METHOD_GET],
            **kwargs,
        )

    websocket_route = websocket
