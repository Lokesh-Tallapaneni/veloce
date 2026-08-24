# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The MCP HTTP transport requires and cross-checks `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` on the `2026-07-28` revision. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `serve_stdio` isolates the protocol wire, so handler or subprocess output cannot corrupt the JSON-RPC stream. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))

### Added

- `TestClient(app, loop=...)` drives the app on a loop you supply, which the client never closes. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `HeaderMismatchError` rejects a modern MCP request whose standard headers disagree with its body. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))

### Changed

- Query strings and urlencoded bodies carrying no percent-escape skip per-field decoding. Measured on one Windows desktop: 20.9 vs 26.0 us for a three-parameter query read, 35.3 vs 43.4 us for a five-field form POST; an escaped value is unchanged. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- The `MAX_CONTENT_LENGTH` header scan matches the name as ASGI mandates it rather than lowercasing every header. A server that sends a differently-cased `Content-Length` loses the early rejection, not the limit. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `TestClient` builds a `SelectorEventLoop` on Windows; pass `loop=asyncio.ProactorEventLoop()` for a handler that spawns a subprocess. Measured on one Windows desktop: 73.0 vs 93.6 us per request. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- An ASGI WebSocket message is read and written one coroutine frame deep when no timeout is set. Measured on one Windows desktop: 1.34 vs 2.02 us per echo round. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- An MCP `list[T]` tool argument refuses a non-array and a wrong-typed member instead of wrapping or passing it through; send the array the published schema declares. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))

### Fixed

- `stream=True` is honored on a blueprint route and on an included router's route. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- A blueprint route keeps its MCP resource mime type, size, annotations and `_meta`. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- Capabilities are advertised per protocol revision, so none is offered and then refused. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `tools/list` omits `execution` for a modern client, whose revision removed the field. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `server/discover` is marked private rather than publicly cacheable; its answer varies by caller. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))
- `subscriptions/listen` works on the default HTTP deployment instead of requiring a persistent session. ([#288](https://github.com/Lokesh-Tallapaneni/veloce/pull/288))

## [0.17.1] - 2026-08-23

### Added

- Benchmarks page: measured throughput against other frameworks, and the method behind the numbers. ([#286](https://github.com/Lokesh-Tallapaneni/veloce/pull/286))

### Changed

- `GZipMiddleware` compresses a buffered body below `min_stream_chunk_offload` inline instead of on the thread pool. Measured at 32 concurrent requests: 12,489 vs 5,393 requests per second for a 6 KiB body, 5,432 vs 4,102 at 32 KiB; past roughly 48 KiB the pool wins and is still used. ([#286](https://github.com/Lokesh-Tallapaneni/veloce/pull/286))

## [0.17.0] - 2026-08-23

### Added

- `VeloceDeprecationWarning` carries every Veloce deprecation and is visible under the default warning filter. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `url_for` is importable from the top level, building a URL against the active app. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `UploadFile.save_async` streams an upload to disk without blocking the event loop. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `WebSocket.accepted_subprotocol` reports the subprotocol the connection settled on. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `Capability`, `Transport`, `BidirectionalTransport` and `register_sse_transport` are exported from `veloce.contrib.mcp`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `MCPContext` exposes `client_id`, `request_id`, `task_id`, `origin_request_id`, `transport` and `lifespan_context`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))

### Fixed

- `TrustedHostMiddleware`, `HTTPSRedirectMiddleware` and `CSRFMiddleware` stand down for a replayed MCP call, which they previously refused. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `add_middleware(instance, name="x")` applies the name, so `exclude_middleware=["x"]` matches it. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- A class-based view receives its path parameters; `MethodView.get(self, request, uid)` no longer raises. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `add_url_rule` registers the verbs a `View` declares instead of defaulting to `GET`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `HTTPSRedirectMiddleware` ignores an `X-Forwarded-Proto` hop that `ProxyFix` refused. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `render_template_string` resolves filters, globals and tests registered on the app. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `TestClient.websocket_connect` sends a `Host` header, which RFC 6455 Sec. 4.1 requires. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- A conditional 304 advertises the representation's length instead of `Content-Length: 0`, including when a handler and a middleware both downgrade it. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- An `after_request` hook is called by its own signature, so one taking only `(response)` works. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- Task augmentation is refused on every method that cannot run in the background, not just two. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `veloce check` and `veloce routes` load the dotenv file, and accept `--env-file` / `--no-env-file`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `CORSMiddleware` merges `Access-Control-Expose-Headers` instead of discarding another middleware's entries. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `BadResetToken` is raised on misuse; it also subclasses `TypeError`, which was raised before. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `Request.is_disconnected()` reports a real disconnect on a `stream=True` route. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `MAX_CONCURRENT_CONNECTIONS` and `WRITE_BUFFER_HIGH_WATER` are seeded in `default_config()`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- A multipart body that ends inside a part is refused with 400 instead of returning 200 with the field missing. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- A multipart upload's spool file is closed once the request is done with it, instead of surviving until collection. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `Request.url_for(..., _external=True)` builds from the request's recovered scheme, host, port and `script_root`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))

### Deprecated

- `Veloce.on_event()` warns through `VeloceDeprecationWarning`; use `@app.on_startup` / `@app.on_shutdown`. Removal in v1.0.0. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `FileResponse(path)` on a running loop warns through `VeloceDeprecationWarning`; use `await FileResponse.from_path(path)`. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))

### Changed

- Deprecation warnings are raised as `VeloceDeprecationWarning` rather than `DeprecationWarning`, which the default filter hid. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `import veloce` no longer imports the MCP, OpenAPI or Redis integrations; they resolve on first use. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `MAX_CONTENT_LENGTH` is enforced once per request by the transport that read the body, instead of again during dispatch. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `add_middleware` raises `TypeError` on a construction argument passed with an already-built instance, instead of dropping it. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- The templating error names `Veloce(template_folder=...)` rather than a private attribute. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `url_for`, `url_path_for` and `Request.url_for` take the endpoint positionally, so a route may have a `{name}` segment. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `CORSMiddleware` sends `Allow-Credentials` and `Expose-Headers` only when an origin was allowed. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `SessionAuth` lets a missing `SessionMiddleware` surface instead of masking it as an anonymous request. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `TestClient` percent-decodes the request path, as an ASGI server does. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- `ToolSearch` tools are counted by the MCP scoped-tool scan. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))
- A class-based view is forwarded only the path parameters its target declares; reading `request.path_params` still works. ([#284](https://github.com/Lokesh-Tallapaneni/veloce/pull/284))

## [0.16.0] - 2026-08-23

### Security

- Replaying an MCP refresh token revokes the whole token family, per OAuth 2.1 Sec. 4.14.2. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A trusted `Forwarded` header is the sole authority for `for`, `proto` and `host`; a hop refused by trust depth can no longer set them through `X-Forwarded-*`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `RateLimitMiddleware` keys on the caller's address under ASGI; a changing `User-Agent` no longer bypasses it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))

### Added

- `MCPContext.request_meta` reads the `_meta` the client sent with this request. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `add_mcp_proxy(scopes=..., tags=...)` puts an upstream's tools behind a scope or a label. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The 26 named HTTP exception classes are exported from the top level. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `VeloceError` roots every exception Veloce raises; every existing base is kept. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `QueryParams`, `Cookies`, `State` and `Address` are exported from the top level. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The ten signals are exported from the top level. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `HealthPlugin` is exported from the top level. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `ServerNotImplemented` names the 501 exception class; `NotImplemented_` still resolves to it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A resource template accepts `{+name}`, binding a whole path - separators included - to one variable. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `veloce mcp run` serves an app's MCP tools, which is what a client config file launches. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `veloce mcp list` prints the tools, resources and prompts a client would see. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPAuthorizationServer` issues MCP tokens: OAuth 2.1 with PKCE, refresh rotation, and RFC 7591 registration. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `register_authorization_server` mounts its metadata, `/authorize`, `/token` and `/register`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `AuthorizationStore` and `InMemoryAuthorizationStore` back the issued clients, codes and tokens. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mount_mcp(transport="sse")` serves the deprecated split-endpoint SSE wire. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mount_mcp(session_backend=...)` shares HTTP MCP sessions between workers. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `SessionBackend` and `SessionRecord` are exported for implementing that store. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mount_mcp(page_size=...)` paginates the MCP list methods with the spec's opaque cursor. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A tool may return a list of content blocks, emitted in order as the result's `content`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.state` reaches the call's request state, so a handler holding the context can stash a value. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mcp_tool(tags=...)` labels a tool, and every tool exposes `tags` for a visibility policy. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `pathlib.Path` parameter declares `format: path` in the tool schema. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mcp_resource_mime_type=` declares the media type a resource listing advertises. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mount_mcp(tool_search=...)` publishes `search_tools`, `describe_tools` and `run_tools` in place of the catalogue. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `run_tools` runs several declared calls in one request, passing results between steps. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mcp_tool(version=...)` registers several versions of a tool under one published name. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.sample_with_tools` runs the sampling loop, executing the tools the model asks for. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `SamplingRun` and `SampledToolCall` report a run's answer, transcript and tool calls. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `derive_tool` and `ArgTransform` publish a narrower façade over a registered tool. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `app.add_mcp_tool` registers an already-built tool. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.hide` / `unhide` / `reset_visibility` narrow one connection's view of the catalogue. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `app.mount(..., expose_mcp=True)` publishes a sub-application's MCP primitives through its parent. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `add_mcp_proxy` serves another MCP server's tools from this app, forwarding each call. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `@app.before_mcp_call` and `@app.after_mcp_call` run around every MCP call, route-backed or not. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.result_meta` attaches `_meta` to the result of the call being handled. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `meta=` on `mcp_tool` / `mcp_prompt` and `mcp_meta=` on a route publish `_meta` on the definition. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mcp_resource_size=` and `mcp_resource_annotations=` declare what a resource listing advertises. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `Veloce(website_url=..., mcp_icons=...)` publishes them in the MCP `serverInfo`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.sample(include_context=...)` asks the client to attach server context to the prompt. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mcp_tool(annotations=...)` declares the behaviour hints a tool with no HTTP verb cannot derive. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `render_template`, `render_template_string` and `stream_template` are exported from `veloce.contrib`. ([#283](https://github.com/Lokesh-Tallapaneni/veloce/pull/283))
- `MCPRequestError` is exported from `veloce.contrib.mcp.transports`. ([#283](https://github.com/Lokesh-Tallapaneni/veloce/pull/283))
- `mount_mcp(tool_filter=...)` narrows which tools `tools/list` reports per caller. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext` reports `session_id`, `client_info`, `client_capabilities` and `is_background_task`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.client_supports(name)` tests an advertised client capability, nested with dots. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.debug`/`info`/`warning`/`error` are shorthands for the matching `log` level. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.read_resource` and `get_prompt` reach the server's own components, scope checks included. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.list_resources` and `list_prompts` enumerate what the list methods report. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.send_notification` sends an arbitrary JSON-RPC notification to the client. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- Cacheable MCP results carry `ttlMs` and `cacheScope` on the modern revision. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `mount_mcp(cache_ttl_ms=...)` sets the freshness hint sent with those results. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `subscriptions/listen` opens a filtered notification stream, replacing `resources/subscribe`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `notify_tools_list_changed()` and `notify_prompts_list_changed()` signal those lists changed. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- MCP tasks are served as the `io.modelcontextprotocol/tasks` extension on the modern revision. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `tasks/update` delivers responses to a task's outstanding input requests. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))

### Changed

- The built-in server buffers a route's body before dispatch unless it declares `stream=True`; declare the flag to keep incremental delivery. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `app.mount(..., expose_mcp=True)` takes its flag as a keyword; passing it positionally is refused. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A stateful connection advertises `listChanged: true` for tools, prompts and resources. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A tool, prompt or resource whose declared `scopes` the caller lacks is no longer listed. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A resource list narrowed by declared scopes is marked private, so a shared proxy cannot reuse it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `resources/read` and `prompts/get` refuse a task-augmented request instead of answering synchronously. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A modern client must declare the tasks extension before a task handle is returned. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `tasks/list` and `tasks/result` are not served to a modern client; `tasks/get` carries the result. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `ping` and `logging/setLevel` are not served to a modern client; both revisions keep their own surface. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A modern client sets its log level per request via `_meta`; a request naming none receives no log notifications. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `tools/list`, `prompts/list` and `resources/list` build each entry once and reuse it. ([#283](https://github.com/Lokesh-Tallapaneni/veloce/pull/283))
- `veloce.app` exports `Veloce`, `URLRule` and `Plugin`; `import *` no longer pulls in stdlib names. ([#283](https://github.com/Lokesh-Tallapaneni/veloce/pull/283))
- A test-client websocket read raises `RuntimeError`, not bare `Exception`, when the peer closes. ([#283](https://github.com/Lokesh-Tallapaneni/veloce/pull/283))

### Fixed

- A `typing_extensions.TypedDict` is recognised as an object shape; it was previously advertised as a string. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `TypedDict` Pydantic cannot adapt on this interpreter falls back to a plain mapping instead of failing the request. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `.js`, `.json`, `.css`, `.svg` and `.wasm` are served with their standard media type regardless of the host registry. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The gunicorn worker warns when its TLS certificate is expired or not yet valid. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A dropped SSE client no longer leaves the in-flight call buffering notifications nobody reads. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- Closing a stdio MCP connection reclaims the tasks it created, including one that never settles. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The gunicorn worker honours `--ssl-version` as a minimum TLS version; a floor below the interpreter default is refused and logged. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `request.scheme` reports `https` on a TLS connection served by the built-in server or the gunicorn worker. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `X-Forwarded-Proto` no longer sets the scheme from a hop `ProxyFix` refused. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A task-augmented `tools/call` is refused on a connection with no session, which previously pinned an unreachable task. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.session_id` is unique across worker processes, so per-client state is no longer shared between unrelated clients. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- Graceful shutdown closes idle keep-alive connections before awaiting the server, so shutdown hooks run instead of being killed. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `request.get_json()` and `request.data` read the body under the built-in server and the gunicorn worker. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A response's ETag, `Last-Modified`, `Expires` and `Vary` are read whatever casing wrote them. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An `If-Match` a response satisfies is no longer refused when its ETag was written as `Etag`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `FileResponse` names its media type through the same memoized lookup the static server uses. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `StaticFiles` honours an `If-Range` ETag a subclass emitted with surrounding whitespace. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A proxied call forwards the caller's `_meta`, so an upstream sees the progress token. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `run_tools` refuses a plan whose step ids repeat instead of mis-resolving a `$from` reference. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `$from` pointer of `/` names the member keyed `""`, as RFC 6901 defines it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An array index in a `$from` pointer is refused unless it is unsigned and unpadded. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A too-deeply-nested step argument fails that step rather than the whole plan. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A proxied result whose content block is not an object no longer ends the plan. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `SamplingRun.messages` ends with the answer, so extending it for another run keeps it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A sampled tool's `structuredContent` reaches the model instead of only its text. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A fractional number is refused where a tool declares an integer, instead of losing its fraction. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A number or a string is refused where a tool declares a boolean. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `derive_tool` refuses to derive from a derived tool, which published a surface no call could satisfy. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `ArgTransform(schema=...)` refuses a type the handler behind it would reject. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A copied tool no longer advertises versions only the tool it was copied from can serve. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A proxied tool drops the upstream's version metadata, which the gateway cannot honour. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A listing a connection can narrow is marked `private`, so a shared cache cannot replay it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.hide` narrows what `search_tools` and `describe_tools` report, not only the listing. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A server that can narrow nothing no longer rebuilds its catalogue on every discovery call. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- Every `MCPError` a tool handler raises reaches the caller with its code, message and `data`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A route-backed tool's `MCPError` is delivered instead of being rendered as an HTTP error body. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `MCPContext.hide` announces only the listing the hidden name belongs to. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `list_changed` notification is no longer sent for a capability `initialize` did not advertise. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An MCP tool result encodes through the framework's own encoder, so both doors answer the same JSON. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `Secret` in a tool result is refused, as it already was on the HTTP path, instead of being emitted. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A model's computed fields survive the `orjson` fallback, matching `jsonable_encoder`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `msgspec.Struct` publishes its fields instead of its Python repr. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The MCP protected-resource metadata advertises `bearer_methods_supported`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `list[Model]` tool parameter publishes the model as its item schema, not a string. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An MCP argument whose JSON type contradicts the published schema is refused, not passed to the handler. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A JSON-RPC response POSTed to the HTTP transport is accepted with `202`, not refused. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- URL-mode elicitation sends the required `elicitationId`, so a conforming client accepts it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- URL-mode elicitation is refused unless the client declared `elicitation.url`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A percent-encoded resource template value reaches the handler decoded. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- The most specific resource template serves a URI, not whichever was registered first. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A path parameter no handler parameter declares is documented in OpenAPI and in the tool schema. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- JSON that is not a Request object is `-32600`, not the `-32700` reserved for unreadable input. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A request carrying a null id is refused; MCP requires a string or integer id. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An MCP call no longer leaves its request bound, which corrupted the HTTP transport's own request. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An abandoned MCP request releases its cancellation-registry entry instead of stranding it. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A tool returning a `@dataclass` or `TypedDict` publishes an `outputSchema` and `structuredContent`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An optional tool parameter advertises its null branch, and a parameter's default is published. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A sub-dependency's body model advertises its fields, so a call built from the schema is accepted. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A tool returning `bytes` reports the decoded text, or base64 when the bytes are not text. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `@dataclass` parameter is validated and passed as the dataclass instead of failing on every call. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A `TypedDict` parameter declares an object schema, matching what the handler accepts. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- `client_host`, `client_port` and `remote_addr` report the peer on the ASGI path. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- An authorization failure inside a tool is reported as forbidden, not as an internal error. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))
- A modern-revision client's identity and capabilities are read from each request's `_meta`. ([#282](https://github.com/Lokesh-Tallapaneni/veloce/pull/282))

## [0.15.0] - 2026-08-20

### Added

- MCP serves the `2026-07-28` revision alongside the handshake revisions, selected per request. ([#276](https://github.com/Lokesh-Tallapaneni/veloce/pull/276))
- `server/discover` advertises the served protocol versions, capabilities, and server identity. ([#276](https://github.com/Lokesh-Tallapaneni/veloce/pull/276))
- An MCP request naming an unserved protocol version is rejected with `-32022` listing what is served. ([#276](https://github.com/Lokesh-Tallapaneni/veloce/pull/276))
- `Query(group=True)` reads a model annotation's fields from the query string. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- `group=True` is accepted by `Header`, `Cookie`, and `Form` for the same field spread. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- `SessionAuth` resolves a cookie session into the request's `Principal`. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- `login_session` and `logout_session` sign a subject in and out, rotating the session id. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- `HealthPlugin` serves `/livez` and `/readyz`, failing readiness once shutdown begins. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- `app.add_lifespan()` registers additional lifespan context managers on the app's exit stack. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))

### Changed

- `Request.content_length` reads the raw header tuples instead of materializing `Headers`. ([#275](https://github.com/Lokesh-Tallapaneni/veloce/pull/275))

### Fixed

- An `HTTPException` reports the same body over MCP and background tasks as it does over HTTP. ([#274](https://github.com/Lokesh-Tallapaneni/veloce/pull/274))
- An MCP tool call with invalid arguments is reported in-band as `isError`, not as a protocol error. ([#277](https://github.com/Lokesh-Tallapaneni/veloce/pull/277))
- An MCP argument-validation message names the offending argument instead of rendering a Python repr. ([#277](https://github.com/Lokesh-Tallapaneni/veloce/pull/277))

## [0.14.0] - 2026-08-19

### Added

- `request` and `csp_nonce` resolve in templates without threading them through the render context. ([#272](https://github.com/Lokesh-Tallapaneni/veloce/pull/272))
- `csp_nonce()` reads the request being handled when called without one. ([#272](https://github.com/Lokesh-Tallapaneni/veloce/pull/272))

### Changed

- `instrument_with_prometheus` reports a collector-name collision with the `registry=` and `prefix=` fixes. ([#272](https://github.com/Lokesh-Tallapaneni/veloce/pull/272))

## [0.13.0] - 2026-08-19

### Added

- `ws.app` exposes the serving application on a WebSocket, mirroring `request.app`. ([#269](https://github.com/Lokesh-Tallapaneni/veloce/pull/269))
- `RateLimitMiddleware(strict_overrides=False)` warns instead of failing on an override key matching no route. ([#269](https://github.com/Lokesh-Tallapaneni/veloce/pull/269))

### Changed

- `WebSocket` declares `__slots__`, cutting per-connection memory; attach data to `ws.state`. ([#270](https://github.com/Lokesh-Tallapaneni/veloce/pull/270))
- `SessionStore` declares `__slots__`, so a slotted store subclass no longer carries a `__dict__`. ([#270](https://github.com/Lokesh-Tallapaneni/veloce/pull/270))

### Fixed

- A parameter marker's default is applied when an MCP tool call omits the field. ([#269](https://github.com/Lokesh-Tallapaneni/veloce/pull/269))
- An MCP tool's `inputSchema` advertises a parameter marker's declared default. ([#269](https://github.com/Lokesh-Tallapaneni/veloce/pull/269))
- `X-RateLimit-Reset` never advertises a wait longer than the configured window. ([#269](https://github.com/Lokesh-Tallapaneni/veloce/pull/269))

## [0.12.1] - 2026-08-16

### Fixed

- A scalar `Body()` parameter is documented in the OpenAPI `requestBody` instead of omitted. ([#267](https://github.com/Lokesh-Tallapaneni/veloce/pull/267))
- `Body(embed=True)` params document one JSON object body, required only when a field is. ([#267](https://github.com/Lokesh-Tallapaneni/veloce/pull/267))
- On Python 3.10, an `Annotated[T, Body()]` parameter defaulting to `None` is read from the body, not the query string. ([#267](https://github.com/Lokesh-Tallapaneni/veloce/pull/267))

## [0.12.0] - 2026-08-16

### Changed

- A handler's return annotation now supplies `response_model`, so the response is filtered to the annotated model. ([#263](https://github.com/Lokesh-Tallapaneni/veloce/pull/263))
- Pass `response_model=None` to keep a model return annotation while declaring no response contract. ([#263](https://github.com/Lokesh-Tallapaneni/veloce/pull/263))

### Added

- `veloce check` reports routes with no response schema and routes whose `response_model` contradicts the return annotation. ([#263](https://github.com/Lokesh-Tallapaneni/veloce/pull/263))
- `app.response_contract_audit()` returns those findings for a pre-deploy script or test. ([#263](https://github.com/Lokesh-Tallapaneni/veloce/pull/263))
- A `list[Model]` return annotation documents an array response and filters its elements. ([#264](https://github.com/Lokesh-Tallapaneni/veloce/pull/264))
- A union return annotation (`A | B`, `A | None`) documents its alternatives as `oneOf`. ([#264](https://github.com/Lokesh-Tallapaneni/veloce/pull/264))
- `Veloce(debug=True)` logs the response-contract findings at startup. ([#264](https://github.com/Lokesh-Tallapaneni/veloce/pull/264))

### Fixed

- A `response_model` subclass instance is re-shaped to the declared model instead of leaking its extra fields. ([#263](https://github.com/Lokesh-Tallapaneni/veloce/pull/263))

## [0.11.0] - 2026-08-03

### Added

- `app.install(plugin)` registers an app extension in one call — any object with an `install(self, app)` method. ([#253](https://github.com/Lokesh-Tallapaneni/veloce/pull/253))
- `name`, when set on a plugin, records it under `app.extensions`. ([#253](https://github.com/Lokesh-Tallapaneni/veloce/pull/253))
- The `/docs` and `/redoc` pages carry the CSP nonce on every script, style, and stylesheet tag. ([#260](https://github.com/Lokesh-Tallapaneni/veloce/pull/260))

### Fixed

- A response emits one header line per field name, so two casings of one header no longer both ship. ([#260](https://github.com/Lokesh-Tallapaneni/veloce/pull/260))
- `RateLimitMiddleware` rejects an override key matching no route at startup, not on every request. ([#260](https://github.com/Lokesh-Tallapaneni/veloce/pull/260))
- `veloce custom` prints the app's CLI group help instead of crashing when no command is given. ([#254](https://github.com/Lokesh-Tallapaneni/veloce/pull/254))

## [0.10.0] - 2026-07-06

### Added

- `@app.query` registers a route for the HTTP `QUERY` method (RFC 10008) — safe and idempotent like `GET`, with a request body like `POST`. ([#244](https://github.com/Lokesh-Tallapaneni/veloce/pull/244))

## [0.9.0] - 2026-06-24

### Added

- `SecurityScheme` is the shared base for authentication schemes, owning `auto_error` and the `__call__(request)` contract. ([#236](https://github.com/Lokesh-Tallapaneni/veloce/pull/236))
- `stream=True` on a route opts its handler into incremental request-body reading via `request.stream()`, instead of buffering the body first. ([#222](https://github.com/Lokesh-Tallapaneni/veloce/pull/222))
- `MCPError` and typed subclasses (`InvalidParamsError`, `AuthorizationError`, others) let an MCP handler raise a specific JSON-RPC error. ([#229](https://github.com/Lokesh-Tallapaneni/veloce/pull/229))
- The MCP HTTP transport rejects an unsupported `MCP-Protocol-Version` header with `400`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `ProtocolVersionError` and `OriginNotAllowedError` surface MCP transport violations as typed errors. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP HTTP endpoint answers a `GET` with `405 Method Not Allowed`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP SSE stream sends a priming event on open and a `retry` field before closing. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP `initialize` result emits `instructions` from the app description or summary. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP `initialize` result emits a `serverInfo.title` from the app title. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- MCP tool annotations now carry `openWorldHint` and the route summary as `annotations.title`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- MCP tool `inputSchema` and `outputSchema` declare the JSON Schema 2020-12 dialect. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `Icon` objects on `@app.mcp_tool`, `@app.mcp_prompt`, and `mcp_icons=` routes surface as a primitive's `icons` array. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- MCP content blocks carry optional `audience` / `priority` / `lastModified` annotations. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `ResourceLink` and `EmbeddedResource` content blocks let a route return a linked or inlined resource result. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `@app.mcp_completer` answers MCP `completion/complete` with per-argument value suggestions for a prompt or resource. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- MCP `notifications/cancelled` cancels the named in-flight request and unwinds its task. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPSession` records the client capabilities advertised in `initialize` over the stdio transport. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCP_ENFORCE_LIFECYCLE` rejects a request that precedes `initialize` on a stateful connection. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `task_support=True` opts an MCP tool into a task-augmented `tools/call` that runs in the background. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- A task-augmented `tools/call` returns a `CreateTaskResult` the client polls for the result. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `tasks/get`, `tasks/result`, `tasks/list`, and `tasks/cancel` drive an MCP task through its lifecycle. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP server emits `notifications/tasks/status` with the related-task `_meta` on each task transition. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `mount_mcp(transport="http", sessions=True)` assigns and validates an `Mcp-Session-Id` on the HTTP transport. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP HTTP transport rejects a missing required session id with `400` and a terminated one with `404`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- A `DELETE` on the MCP HTTP endpoint terminates the session when session management is enabled. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `SessionRequiredError` and `SessionNotFoundError` surface MCP session violations as typed errors. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `mount_mcp(transport="http", resumable=True)` attaches per-stream ids to MCP SSE events and keeps a bounded replay buffer. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- A `GET` carrying `Last-Event-ID` resumes an MCP SSE stream, replaying only that stream's missed events. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCP_RESOURCE_SUBSCRIPTIONS` lets a client `resources/subscribe` and `resources/unsubscribe` to a resource URI. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPServer.notify_resource_updated` sends `notifications/resources/updated` to subscribed connections. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPServer.notify_resources_list_changed` sends `notifications/resources/list_changed` to open connections. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPContext.sample` asks the client's model for a completion via `sampling/createMessage`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPContext.elicit` requests user input via `elicitation/create` in form or URL mode. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPContext.roots` lists the client's filesystem roots via `roots/list`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The stdio transport issues server-to-client requests and awaits their correlated replies. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPCapabilityError` rejects a server-initiated request the client did not advertise support for. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP `resources` capability advertises `subscribe` and `listChanged` when subscriptions are enabled. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- MCP resource subscriptions deliver `notifications/resources/updated` over a stateful HTTP `Mcp-Session-Id` connection. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP HTTP transport records the client capabilities from `initialize` on a session, gating `MCPContext.sample` / `elicit` / `roots`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))

### Changed

- A client disconnecting from an MCP SSE stream no longer cancels the in-flight call. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCPContext.cancelled` reflects real cancellation state instead of always returning `False`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP HTTP transport advertises `resources.subscribe` / `listChanged` as `true` only with `sessions=True`; a stateless request advertises `false`. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `MCP_ENFORCE_LIFECYCLE` is enforced on a stateful HTTP `Mcp-Session-Id` connection, not only over stdio. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `Response.mimetype`, `charset`, and `mimetype_params` cache their parse, keyed on the current `content_type` value. ([#239](https://github.com/Lokesh-Tallapaneni/veloce/pull/239))
- Route registration rejects a path parameter name that is not a valid Python identifier or is a reserved keyword, instead of failing opaquely at request time. ([#240](https://github.com/Lokesh-Tallapaneni/veloce/pull/240))

### Fixed

- Response and DI-injected background tasks are tracked and cancelled-and-drained on shutdown, so one no longer outlives the event loop and is orphaned mid-run. ([#241](https://github.com/Lokesh-Tallapaneni/veloce/pull/241))
- `request.json()` caches a JSON `null` body as `None` so it is parsed once instead of re-decoded on every access. ([#240](https://github.com/Lokesh-Tallapaneni/veloce/pull/240))
- The MCP HTTP `GET` resume path validates `Origin` and `MCP-Protocol-Version` so a cross-origin or unsupported-version client cannot bypass the DNS-rebinding defense. ([#237](https://github.com/Lokesh-Tallapaneni/veloce/pull/237))
- MCP `completion/complete` bounds the number of client-supplied `context.arguments` entries it ingests. ([#237](https://github.com/Lokesh-Tallapaneni/veloce/pull/237))
- A malformed inbound `traceparent` no longer raises out of the OpenTelemetry span-emit hook; the span is rooted instead. ([#237](https://github.com/Lokesh-Tallapaneni/veloce/pull/237))
- An MCP task that settles after a racing `tasks/cancel` keeps its `cancelled` status instead of being overwritten. ([#237](https://github.com/Lokesh-Tallapaneni/veloce/pull/237))
- MCP `notifications/cancelled` ignores a non-scalar `requestId` instead of raising `TypeError` on the lookup. ([#237](https://github.com/Lokesh-Tallapaneni/veloce/pull/237))
- `PlainTextResponse` and `HTMLResponse` now accept `bytes` as well as `str`, matching Starlette parity. ([#226](https://github.com/Lokesh-Tallapaneni/veloce/pull/226))
- An MCP HTTP client's `notifications/cancelled` cancels only its own in-flight request, never a peer's call with a colliding JSON-RPC id. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- An MCP task is private to the connection that created it; `tasks/list` and `tasks/get` / `result` / `cancel` reject another connection's task. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP HTTP session store evicts idle `Mcp-Session-Id` sessions so an abandoned session no longer leaks for the process lifetime. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- The MCP SSE event store caps retained streams so a long-running resumable server's replay buffer no longer grows without bound. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- An MCP task keys ownership to a stable per-connection id so a task cannot alias to a later session that reuses a freed session's address. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- Evicting an MCP HTTP session cancels and drops its tasks so a never-settling task no longer pins memory for the process lifetime. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `tasks/cancel` delivers its `notifications/tasks/status` (cancelled) reliably instead of dropping it to garbage collection. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- Concurrent MCP SSE streams on one `Mcp-Session-Id` each receive resource-update notifications and unregister independently. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- `mount_mcp(transport="http")` rejects a `task_support` tool without `sessions=True` so a created task is never silently unretrievable. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))
- An MCP task runner refuses `ctx.sample` / `elicit` / `roots` on stdio, settling the task failed instead of racing the serve loop's reader. ([#230](https://github.com/Lokesh-Tallapaneni/veloce/pull/230))

## [0.8.0] - 2026-06-13

### Added

- `Veloce.run(reload=True)` and `veloce run --reload` auto-restart the built-in server on source changes, without uvicorn. ([#212](https://github.com/Lokesh-Tallapaneni/veloce/pull/212))
- `EVENT_LOOP_WATCHDOG` names the route and dependency a blocking call stalled in. ([#210](https://github.com/Lokesh-Tallapaneni/veloce/pull/210))

## [0.7.0] - 2026-06-12

### Changed

- OpenAPI parameters derive from the handler plan the resolver runs, keeping documented and enforced contracts in lockstep. ([#205](https://github.com/Lokesh-Tallapaneni/veloce/pull/205))
- `click` is now an optional `cli` extra; install `veloceframework[cli]` to use `app.cli` and `test_cli_runner`. ([#206](https://github.com/Lokesh-Tallapaneni/veloce/pull/206))

### Fixed

- Parameters the resolver treats as optional are documented as `required: false`, matching runtime. ([#205](https://github.com/Lokesh-Tallapaneni/veloce/pull/205))
- A form request body whose every field is optional is documented as not required, matching runtime. ([#205](https://github.com/Lokesh-Tallapaneni/veloce/pull/205))
- `FileResponse.from_path` emits a bare `Content-Disposition` for a non-default disposition with no filename, matching the sync constructor. ([#207](https://github.com/Lokesh-Tallapaneni/veloce/pull/207))

## [0.6.0] - 2026-06-10

### Added

- `veloce new NAME [--template minimal|api|web]` scaffolds a project, and `veloce generate KIND NAME` (alias `g`) emits a single file. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `get_flashed_messages` is auto-injected as a Jinja global, so templates call it without manual registration. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `SessionMiddleware` / `ServerSessionMiddleware` resolve unset constructor arguments from `app.config` on the first request. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- `app.secret_key` is a live property bound to `config["SECRET_KEY"]`, so it alone configures `SessionMiddleware`. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- `send_file` / `async_send_file` apply `SEND_FILE_MAX_AGE_DEFAULT` when called without `max_age=`. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))

### Security

- `CORSMiddleware(allow_origin_regex=...)` gates strictly by the regex instead of defaulting `allow_origins` to `["*"]`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))

### Changed

- `APIRouter` now aliases `Router` (was `Blueprint`); construct `Blueprint` for a named route group. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- `MAX_CONTENT_LENGTH` defaults to `104857600` (100 MiB); set it to `None` for unlimited. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- A failing `yield`-dependency teardown now reaches `got_request_exception` and re-raises under `PROPAGATE_EXCEPTIONS`. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))

### Fixed

- `/docs` renders with `BaseLayout` instead of the unloaded `StandaloneLayout`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `@rate_limit` is honored on `include_in_schema=False` routes (with the strategy API). ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `@app.endpoint(name)` reclassifies the route so a sync view is offloaded, not awaited. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- `PROPAGATE_EXCEPTIONS=false` (and `0`/`off`) from an env file now reads as off. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- `security_audit()` no longer claims session signing falls back to weak defaults when `SECRET_KEY` is unset. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- The native server drops chunked-request trailer fields instead of prepending them to the next request. ([#198](https://github.com/Lokesh-Tallapaneni/veloce/pull/198))
- A mounted sub-app's trailing-slash redirect carries the mount prefix in its `Location`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- A non-ASCII `query_string` over ASGI returns `400` instead of raising a `500`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- A `multipart/form-data` body that fails mid-parse returns `400`, not a partial `200`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `StreamingResponse` on the native server no longer truncates on an empty `bytes` chunk. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- Registering `/users` and `/users/` no longer flips the first to a slash redirect. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- Blueprint routes keep `exclude_middleware=[...]` after `register_blueprint`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- A mutable parameter default (`tags: list[str] = []`) is no longer shared across requests. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- `ProxyFix` keeps the brackets and port of a `Forwarded` IPv6 `host`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- A native-server `HEAD` response no longer sends a body, keeping `Content-Length`. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- The native server no longer drops a WebSocket frame pipelined into the handshake segment. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))
- A non-WebSocket `Upgrade` (e.g. `h2c`) returns `400` without running the route handler. ([#197](https://github.com/Lokesh-Tallapaneni/veloce/pull/197))

## [0.5.0] - 2026-06-10

### Added

- MCP HTTP transport hardening: `mount_mcp(transport="http", allowed_origins=[...])`
  validates the `Origin` header (DNS-rebinding defense), and
  `exclude_middleware=[...]` drops named app middleware from the `/mcp` + metadata
  routes (so an app-wide auth middleware the transport's own `auth` replaces does
  not run on it). ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP authorization: `mount_mcp(transport="http", auth=MCPAuth(...))` makes the
  endpoint an OAuth 2.1 resource server — a user-supplied `verify` callable
  validates the bearer token on every request, the RFC 9728 protected-resource
  metadata is served, and a missing/invalid token returns `401` (insufficient
  endpoint scope returns `403`) with a `WWW-Authenticate` challenge. Declarative
  per-tool scopes (`@app.mcp_tool(scopes=...)`, `mcp_scopes=` on exposed routes)
  are enforced against the request principal. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- `Principal` + `current_principal()` / `set_principal()`: a unified authenticated
  identity populated by HTTP auth or the MCP transport, so authorization and
  identity-aware dependencies read one source across both doors. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- `Request.is_mcp` marks a replayed MCP tool/resource call, so auth middleware can
  defer to the transport on agent calls while business middleware runs unchanged. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP Streamable HTTP transport: `app.mount_mcp(transport="http", path="/mcp")`
  mounts the MCP server as a `POST` route, so it can run as a remote/hosted server
  under any ASGI server. A request with `Accept: text/event-stream` is answered with
  an SSE stream of the call's progress/log notifications followed by the JSON-RPC
  response; otherwise a single JSON response. The route is protected by whatever
  middleware and dependencies the app applies to it. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP progress and logging: `MCPContext.report_progress(...)` and
  `MCPContext.log(...)` now send live `notifications/progress` and
  `notifications/message` to the client (progress requires the client's
  `progressToken`); the server handles `logging/setLevel` and advertises the
  `logging` capability. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP per-call timeout: set `app.config["MCP_CALL_TIMEOUT"]` (seconds) to bound each
  tool call, resource read, and prompt render; an overrun is cancelled and surfaced
  as an in-band tool error or a JSON-RPC error. Unset (no timeout) by default. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP prompts: register a reusable prompt template with `@app.mcp_prompt(...)`. The
  callable's parameters become the prompt's arguments and its return (a string or a
  list of role/content messages) becomes the rendered messages; the server answers
  `prompts/list` and `prompts/get`, with `Depends`/`MCPContext` resolved as in a
  tool, and advertises the `prompts` capability when at least one is registered. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP resources: expose a read-only (`GET`/`HEAD`) route as a Model Context
  Protocol resource with `expose_as_mcp_resource=True` and `mcp_resource_uri=...`
  (a static URI, or a URI template such as `users://{user_id}` binding the route's
  path parameters). The server answers `resources/list`, `resources/templates/list`,
  and `resources/read`, replaying the route's dependencies, security, and
  `response_model` through the shared invocation path; it advertises the
  `resources` capability when at least one resource is registered. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP non-text tool content: a tool returning an `image/*` or `audio/*` response
  emits the matching typed MCP content block (base64), and a binary resource read
  returns its bytes as a `blob`. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))

### Fixed

- The native dev server (`app.run()`) now starts on Windows: `reuse_port` is
  requested only where `SO_REUSEPORT` exists, instead of unconditionally passing
  `reuse_port=True` to the selector event loop (which raised `ValueError` and
  killed the serving thread before it bound). ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- The native dev server now drains in-flight requests on shutdown on Windows too:
  where `loop.add_signal_handler` is unavailable, `_serve` falls back to
  `signal.signal` and schedules the cooperative shutdown on the loop, so Ctrl+C /
  Ctrl+Break let an in-flight request finish at its boundary instead of raising
  `KeyboardInterrupt` straight out of the loop and resetting the connection. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- Blueprint error handlers are now scoped to their own routes: a
  `@bp.errorhandler` only catches exceptions raised on that blueprint (or a nested
  descendant), consulted by the failing request's blueprint chain before the
  app-level handlers — it no longer catches a sibling blueprint's or an app-level
  route's exception. `error_handler_spec` now reports per-blueprint sub-tables. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- A mounted Veloce sub-app now sees `request.root_path` (and `script_root`) set to
  its mount prefix, matching mounted ASGI apps, so `url_for` and proxy-aware URLs
  inside the sub-app are prefix-correct. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- `JSONResponse`, `HTMLResponse`, and `PlainTextResponse` accept `background=`
  (forwarded to the base `Response`), so a `BackgroundTask`/`BackgroundTasks` can
  be attached to them as it can to `Response`. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- `FileResponse(content_disposition_type="inline")` now emits
  `Content-Disposition: inline` even without a `filename`; an explicit non-default
  disposition is honoured (the default `attachment` without a filename still emits
  no header, so plain file responses are not forced to download). ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- The `session` proxy forwards attribute writes, so `session.permanent = True`
  works through the global proxy rather than raising `AttributeError`. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- A single Pydantic body model's validation errors are now located under `"body"`
  (e.g. `["body", "field"]`), consistent with `Body(...)` marker params and the
  whole-body error cases. ([#195](https://github.com/Lokesh-Tallapaneni/veloce/pull/195))
- MCP: the `logging/setLevel` minimum is now scoped per request (a ContextVar like
  the progress/notification channel) rather than on the shared `MCPServer`, so one
  HTTP client's level change no longer raises the notification floor for others. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP: a resource read short-circuited by an auth guard (`401`/`403`) maps to a
  forbidden error rather than an internal error. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))

### Security

- MCP: a pure `@app.mcp_tool` handler error (and the defensive internal-error path)
  surfaces a generic message unless `app.debug` is set, so an exception carrying a
  secret is not returned verbatim to the agent. ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))
- MCP: a tool argument can no longer masquerade as an `Authorization`/`Cookie`
  header on the replayed request, so a `Security` scheme cannot read agent-supplied
  input as a credential; `Principal.token` is excluded from `repr()`; `MCPAuth`
  requires `resource_server_url` + `authorization_servers`; and an insufficient
  scope is reported uniformly across tools/resources/prompts (HTTP 403 with a
  `WWW-Authenticate` challenge over the JSON transport). ([#194](https://github.com/Lokesh-Tallapaneni/veloce/pull/194))

## [0.4.0] - 2026-06-08

### Added

- Configurable rate limiting: selectable algorithms (`FixedWindow`,
  `SlidingWindow`, `TokenBucket`), pluggable in-memory or Redis backends, and
  per-route limits via `overrides` or the `@rate_limit` decorator.
- Result caching: the `cached` decorator with `InMemoryCache` and `RedisCache`. ([#171](https://github.com/Lokesh-Tallapaneni/veloce/pull/171))
- `veloce.contrib.redis`: `RedisSessionStore`, `RedisRateLimitBackend`, and
  `RedisCache` for state shared across workers.
- msgspec as an opt-in fast validation and serialization backend. ([#157](https://github.com/Lokesh-Tallapaneni/veloce/pull/157))
- Model Context Protocol integration (`veloce.contrib.mcp`): tool exposure over
  stdio, protocol-version negotiation, `ping`, route-derived tool metadata, and
  streaming-result tools.
- JSON Web Tokens (`encode_jwt` / `decode_jwt`), storage-free reset tokens
  (`make_reset_token` / `check_reset_token`), and a `Secret` wrapper that resists
  accidental disclosure. ([#139](https://github.com/Lokesh-Tallapaneni/veloce/pull/139))
- `CSPMiddleware` (Content-Security-Policy with a per-request nonce and
  report-only mode) and `ConditionalGetMiddleware` (`304` for `If-None-Match` /
  `If-Modified-Since`). ([#139](https://github.com/Lokesh-Tallapaneni/veloce/pull/139))
- `CORSMiddleware` gains Private Network Access support and preflight-method
  validation; `CSRFMiddleware` gains Origin verification via `trusted_origins`. ([#136](https://github.com/Lokesh-Tallapaneni/veloce/pull/136))
- Middleware ordering with `add_middleware(..., priority=N)` and per-route
  opt-out with `exclude_middleware=[...]`.
- Background-task supervision: `app.supervise(...)` (restart policy) and
  `app.spawn(...)` (app-scoped tasks).
- Routing: constrained converter syntax (`{x:converter(arg)}`), `date` / `time` /
  decimal path converters, duplicate-route detection, and the declarative
  `@app.websocket_listener` route.
- `StaticFiles`: precompressed-sibling serving, `html=True` directory indexes,
  and write-side `If-Match` / `If-Unmodified-Since` preconditions.
- WebSockets: native-transport server support on `Veloce.run()`, an idle-receive
  timeout, async-context-manager support, heartbeats, send backpressure, and
  UTF-8 / close-frame validation.
- Server-Sent Events: `ServerSentEvent.comment` and `.json`, bare-value source
  iterators, and a proactive heartbeat.
- Observability: `instrument_access_log` / `log_requests_as_json`, a Prometheus
  exporter (`instrument_with_prometheus`), and an OpenTelemetry bridge with a
  live-tracing mode and an `on_span` hook.
- OpenAPI: separate request/response schemas, identity-keyed components,
  operationId de-duplication, a documented `422` response, and a
  `validate_openapi` flag.
- Sessions: sliding expiry, `domain=` / chunked-cookie options, and
  `Vary: Cookie` on cookie-varying responses.
- Encoder extensibility: a per-call `custom_encoder`, process-level
  `register_encoder`, and broader built-in coverage (`bytes`, `set`/`frozenset`,
  `pathlib.Path`, `re.Pattern`, scalar subclasses).
- Deployment: optional gunicorn `VeloceWorker`, built-in dev-server TLS, an
  ASGI-app `mount`, `.env` loading, a dev event-loop watchdog, and an async
  `TestClient`. `uvicorn` is now an optional extra rather than a hard dependency.
- New top-level exports: `Config`, `Aborter`, `URLRule`, `SetupError`,
  `JSONProvider` / `DefaultJSONProvider` / `config_orjson_options`,
  `get_openapi_schema` / `setup_openapi_routes`, `StaticFiles`,
  `Jinja2Templates`, `log_requests_as_json`, and `async_send_file`.
- Developer documentation: a build-one-app tutorial, a runnable `examples/`
  directory, a databases guide, and a Hypothesis fuzzing harness across the
  parsers, router, signing, and WebSocket paths.

### Changed

- The deprecated `Veloce.on_event()` / `Veloce.add_event_handler()` now target
  removal in `1.0.0`. ([#173](https://github.com/Lokesh-Tallapaneni/veloce/pull/173))
- `Veloce.run(workers=...)` raises `ValueError` for any worker count other than
  `1` (the built-in server is single-process). ([#166](https://github.com/Lokesh-Tallapaneni/veloce/pull/166))
- Independent dependencies resolve concurrently, and a no-wave `Depends` chain
  compiles to a straight-line async resolver. ([#154](https://github.com/Lokesh-Tallapaneni/veloce/pull/154))
- Numerous per-request and schema-generation paths were optimized — a compiled
  feature pipeline, indexed route/encoder lookups, and bounded caches — without
  changing public behavior.
- Route resolution gates its mounted-app, static-handler, and ASGI-mount scans on
  the compiled pipeline flags, skipping each scan when nothing of that kind is
  registered. ([#183](https://github.com/Lokesh-Tallapaneni/veloce/pull/183))
- Literal request paths resolve through a registration-time exact-match map in one
  hash lookup instead of a radix-tree walk, falling through to the tree for
  parameterized, wildcard, and slash-redirect routes (literal `match()` ~1.7x
  faster, ~3x on deep literal paths). ([#185](https://github.com/Lokesh-Tallapaneni/veloce/pull/185))
- Requests to feature-free apps take a straight-line dispatch fast path: when no
  middleware, request/response hooks, mounts, or url-value preprocessors are
  registered and the matched route is an async trivial or request-only handler
  with no response model, custom response class, non-default status, host or
  subdomain constraint, defaults, or middleware exclusion, the middleware, hook,
  route-resolution, and dependency-resolution orchestration is skipped while
  coercion, `after_this_request` callbacks, background tasks, exception handling,
  and teardown remain shared (~6-8% lower per-request dispatch time on those
  routes, in-process A/B). ([#185](https://github.com/Lokesh-Tallapaneni/veloce/pull/185))

### Fixed

- Per-route rate-limit state now rebuilds when routes are added after startup. ([#178](https://github.com/Lokesh-Tallapaneni/veloce/pull/178))
- A bodiless status (`1xx`, `204`, `205`, `304`) no longer advertises a body, the
  WebSocket handshake uses the correct RFC 6455 GUID, and a frame with a non-zero
  RSV bit is rejected.
- `HTTPBasic` / `HTTPDigest` escape the `realm`, non-latin-1 header values are
  RFC 2047 encoded, and `decode_jwt` rejects an empty secret.
- JSON serialization handles `set`/`frozenset`, `pathlib.Path`, integer-valued
  `Decimal`, and `exclude_none`; `StaticFiles` precompressed selection returns
  `406` and honours an explicit `q=0`.
- Assorted correctness fixes across OpenAPI dual-schema comparison, scope-aware
  dependency caching, `instrument_with_otel` idempotency, signal delivery, and
  per-route middleware-exclusion symmetry.

### Security

- `LoggingMiddleware` and the access log escape control characters in
  request-derived fields (CWE-117 log forging), and `RequestIDMiddleware`
  sanitizes an inbound request id.
- Security headers are matched case-insensitively so a handler override is not
  silently replaced; the cookie writer round-trips a literal `%`.
- The native WebSocket server rejects an unmasked client frame, HTTP Basic
  rejects an RFC 7617-malformed credential, and `dump_cookie` rejects a
  non-token cookie name.
- `safe_join` rejects Windows reserved device names, `URL.from_request`
  validates the `Host` header (RFC 3986), and the router rejects a path that
  binds one parameter name twice.

## [0.3.0] - 2026-06-01

### Fixed

- `StaticFiles` now applies RFC 9110 `If-Range` validation correctly before
  serving partial responses. ([#128](https://github.com/Lokesh-Tallapaneni/veloce/pull/128))

## [0.2.0] - 2026-05-31

### Added

- Streaming request bodies on the built-in HTTP server, so large uploads no
  longer require buffering the full body before dispatch. ([#106](https://github.com/Lokesh-Tallapaneni/veloce/pull/106))
- CLI plugin discovery, `.env` loading, template streaming, SSE heartbeat
  support, OpenTelemetry integration, and a signal namespace helper.
- Hybrid routing for patterns that do not fit the radix tree, plus an optional
  gunicorn worker.
- Broader documentation coverage across configuration, templates, static files,
  sessions, signals, and related framework guides.

### Changed

- Request body access is now asynchronous: `request.body()`, `request.text()`,
  and `request.get_data()` must be awaited. ([#106](https://github.com/Lokesh-Tallapaneni/veloce/pull/106))
- `request.stream()` now streams on the raw HTTP path instead of replaying an
  already-buffered body. ([#106](https://github.com/Lokesh-Tallapaneni/veloce/pull/106))
- Debug mode renders an HTML traceback page for clients that prefer HTML while
  preserving plain-text tracebacks for CLI and programmatic clients. ([#117](https://github.com/Lokesh-Tallapaneni/veloce/pull/117))
- Resolver and response-encoding internals were consolidated and optimized
  without changing the public API.

### Fixed

- Correct handling for `If-Range`, partial-content gzip behavior, async template
  context processors, duplicate response headers, hybrid-router edge cases, and
  gunicorn worker lifecycle/TLS behavior.

### Security

- Restored strict header validation on streamed responses.
- Applied the same form-field limits to URL-encoded bodies as multipart forms.

## [0.1.4] - 2026-05-25

### Changed

- Focused maintenance release covering security hardening, correctness fixes,
  API cleanup, and small internal consolidations.
- Improved encoder behavior, cached more parsed request metadata, and reduced
  duplicated logic across middleware, CLI helpers, templating, and the test
  client. ([#95](https://github.com/Lokesh-Tallapaneni/veloce/pull/95))

### Security

- Tightened multipart UTF-8 validation, `HTTPBasic` challenge construction, and
  exception handling around basic-auth parsing. ([#95](https://github.com/Lokesh-Tallapaneni/veloce/pull/95))
- Made HSTS subdomain coverage opt-in rather than implicit. ([#95](https://github.com/Lokesh-Tallapaneni/veloce/pull/95))

### Removed

- Dropped unused internal constants from the handler-plan implementation. ([#95](https://github.com/Lokesh-Tallapaneni/veloce/pull/95))

## [0.1.3] - 2026-05-23

### Changed

- Security and correctness release covering CSRF token rotation, password-hash
  parameter validation, and several framework/runtime fixes.
- Improved diagnostics around OpenAPI schema generation and clarified the
  process-local scope of the built-in rate limiter. ([#94](https://github.com/Lokesh-Tallapaneni/veloce/pull/94))

### Fixed

- Addressed loop-affinity issues in `Veloce()`, multipart encoding in the test
  client, stale response-encode caches, router merge behavior, and several
  runtime guards that previously relied on `assert`. ([#94](https://github.com/Lokesh-Tallapaneni/veloce/pull/94))

### Security

- Added CSRF token rotation support after login or privilege changes. ([#94](https://github.com/Lokesh-Tallapaneni/veloce/pull/94))
- Rejected weak or tampered scrypt parameters during password verification. ([#94](https://github.com/Lokesh-Tallapaneni/veloce/pull/94))
- Added SRI protection for Swagger UI and ReDoc assets. ([#94](https://github.com/Lokesh-Tallapaneni/veloce/pull/94))

## [0.1.2] - 2026-05-23

### Added

- Top-level exports for `render_template`, `render_template_string`, and
  `Jinja2Templates`. ([#78](https://github.com/Lokesh-Tallapaneni/veloce/pull/78))

### Changed

- `Request.json()` became asynchronous for consistency with the rest of the
  request-body API. ([#78](https://github.com/Lokesh-Tallapaneni/veloce/pull/78))
- Runtime dependencies were corrected so standard installs include the pieces
  needed for documented framework features. ([#78](https://github.com/Lokesh-Tallapaneni/veloce/pull/78))
- `veloce.__version__` now comes from installed package metadata. ([#78](https://github.com/Lokesh-Tallapaneni/veloce/pull/78))

## [0.1.1] - 2026-05-23

### Changed

- Metadata-only release correcting maintainer information in the published
  package.

## [0.1.0] - 2026-05-23

### Added

- Initial public release of Veloce as `veloceframework`.
- Core framework surface including the `Veloce` app, radix-tree routing,
  request/response primitives, dependency injection, OpenAPI generation, and
  an in-memory `TestClient`.
- Built-in middleware, sessions, templating, signals, background tasks,
  Server-Sent Events, WebSockets, security helpers, and class-based views.
- CLI commands, static-file support, instrumentation hooks, server-side
  sessions, async password helpers, and the first round of performance-focused
  hot-path improvements.

### Changed

- Set safer defaults and improved consistency across response handling,
  multipart uploads, WebSocket dependency injection, and request streaming.

### Fixed

- Corrected early issues in blueprint registration, SSE encoding, dependency
  coercion, multipart cleanup, session-store race handling, static-file caching,
  logging, and request-scoped resource cleanup.

### Security

- Added incremental request-size enforcement, request timeouts, WebSocket origin
  checks, security headers, signed CSRF tokens, multipart limits, and secure
  deployment audit helpers.
