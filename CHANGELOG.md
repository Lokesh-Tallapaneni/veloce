# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.20.0] - 2026-09-02

### Security

- A route whose unresolved annotation carries any parameter marker is refused, not served unguarded. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- A websocket listener whose message annotation cannot be resolved is refused, not left unvalidated. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- `@app.mcp_tool`, `@app.mcp_prompt`, `MCPAuth` and `MCPAuthorizationServer` reject a bare string scope. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))

### Changed

- A route whose unresolved annotation carries a parameter marker now raises at registration; import the name at runtime. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- A websocket listener whose message annotation cannot be resolved now raises at registration; import the name at runtime. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- `Security(scopes=...)` rejects a bare string; pass `["scope"]` for a single scope. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))

### Fixed

- A parameter marker is found when the annotation's subscript base does not resolve. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- `X | None` carrying an unresolved name behaves as `Optional[X]` does. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- An annotation whose only unresolved name is a `typing` one no longer refuses to register. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- A path parameter with an unresolved annotation registers and reads from the path. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))
- A `functools.partial` handler's pre-bound parameter no longer refuses to register. ([#296](https://github.com/Lokesh-Tallapaneni/veloce/pull/296))

## [0.19.0] - 2026-09-02

### Added

- `@app.websocket_listener` validates each frame against the callback's message annotation. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- A websocket frame that does not match the declared message type closes with `1007`. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- An undiscriminated websocket message union is refused at registration. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- A listener's return annotation documents what the channel sends. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- A websocket listener validates a dataclass or `TypedDict` message, as the HTTP body path does. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))

### Changed

- `Response(background=...)` accepts a bare callable and rejects an unsupported value. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- The `fast` extra requires `msgspec>=0.16`; earlier versions lack `msgspec.convert`. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))

### Fixed

- `MAX_CONCURRENT_CONNECTIONS = None` runs the built-in server without a cap, as documented; it raised `TypeError` and refused every connection. ([#294](https://github.com/Lokesh-Tallapaneni/veloce/pull/294))
- A dataclass or `TypedDict` response model is documented with its own component schema. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- A streaming response from an async generator encodes `str` chunks, as the sync path already did. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- `Request.session` is typed as `Session`, exposing `permanent` and `modified` to type checkers. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- `set_cookie` and `dump_cookie` declare the `expires` types they already accept. ([#295](https://github.com/Lokesh-Tallapaneni/veloce/pull/295))
- A handler may return `(body, headers)` with any mapping; `Headers` is not a `dict`, so the framework's own header type was read as a status and answered `500`. ([#292](https://github.com/Lokesh-Tallapaneni/veloce/pull/292))

## [0.18.0] - 2026-08-31

### Security

- The cold ASGI emit path applies the response-splitting guard to `content_type`; a CR or LF in it reached the wire as a raw header value. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An unresolvable annotation no longer erases the whole signature's PEP 593 metadata; a route whose unrelated parameter had a bad annotation stopped enforcing its `Depends()` security scheme and served unauthenticated. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Response` copies the `headers` mapping it is given; a handler reusing one dict across requests shipped a previous request's `Set-Cookie`, leaking another user's session. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `ProxyFix` counts `Forwarded:` hops correctly when an element carries a backslash; one outside a quoted string hid the following comma, merging the trusted proxy's element into the client's and putting attacker text into `request.remote_addr`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The built-in server serves a request whose target is split across two reads; it answered `400`, and measured `MAX_URL_SIZE` against the last fragment rather than the whole target. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `request.authorization` and `HTTPBasic` decode a `Basic` payload through one implementation, so a header with extra whitespace no longer yields credentials from one and a `401` from the other. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HttpSessionStore` bounds live MCP sessions with `max_sessions` (default `10_000`); the idle TTL limited how long a session lived, not how many a client could mint. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The built-in server's `413` for a declared over-limit `Content-Length` runs the response phase, so it carries the CORS and security headers the ASGI path already gave it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `StaticFiles` streams a byte range at or above `STREAM_THRESHOLD`; `Range: bytes=0-` previously read the whole file into memory. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The built-in server stops reading a connection once `MAX_PIPELINED_REQUESTS` (default `64`) requests are queued, bounding what a pipelining flood can allocate. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `RateLimitMiddleware(max_requests=...)` bounds its per-client state with `max_keys` (default `100_000`), matching the `strategy=` path. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP task `ttl` is clamped to one hour; a client could otherwise pin a task and its result for the process lifetime. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `request.authorization` reports no username for a colon-less `Basic` payload, which RFC 7617 makes malformed; it previously named one for a header `HTTPBasic` answers with a `401`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response_model` filters a msgspec struct or `list[Struct]` response as it filters a Pydantic one; it previously shaped nothing on that backend, so a subclass returned under a base-model contract put its extra fields on the wire. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A credential carrying a non-ASCII byte is refused rather than crashing the request: `decode_jwt` raises `InvalidTokenError`, a forged CSRF token answers `403`, and a PKCE verifier is rejected. All three answered `500` before, pre-authentication. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CORSMiddleware` emits `Vary: Origin` on every response whose allowed origin could depend on the request, including refusals, so a shared cache cannot serve an unkeyed response to an allowed origin. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CSPMiddleware` raises `ValueError` rather than asserting when given no policy, so `python -O` cannot leave it constructed and emitting no header. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Runtime dependency floors raised to releases carrying current security fixes: `orjson>=3.11.5`, `pydantic>=2.4.0`, `python-multipart>=0.0.22`, `jinja2>=3.1.6`, `gunicorn>=23.0.0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `dependencies=` entry that is not a `Depends` raises `TypeError` at registration. `dependencies=[guard]` - the `Depends()` wrapper forgotten - was silently discarded, so the guard never ran and every route on it was open. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The SSE transport runs a tool as the principal that authenticated the `POST`, not the one that opened the `GET` stream. A validated token for one caller executed under another caller's scopes. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP HTTP endpoint authenticates and `Origin`-checks `GET` and `DELETE`, not only `POST`. A resuming `GET` replayed another principal's tool output without a credential, and any origin could terminate a session. Clients must now send their token on both verbs. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A JSON body model refuses a body whose `Content-Type` declares it is not JSON, closing a CSRF avenue: `text/plain` and the form types are sent cross-origin without a CORS preflight. An absent header and a `+json` suffix are still accepted. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `await request.json()` applies that same rule and reads a declared non-JSON body as `None`, closing the avenue for handlers that parse the body themselves. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HTTPException` copies the headers it is given, so a security scheme's cached `WWW-Authenticate` challenge cannot accumulate one request's `Set-Cookie` or CORS headers and ship them on the next `401`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A request inside a mounted sub-app reports the connection's scheme and client address; `request.is_secure` read `False` over TLS and `request.client_host` read `None`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HTTPSRedirectMiddleware` no longer redirects a request that arrived over TLS, which looped when the app served TLS itself or a proxy spelled the scheme in any casing but lowercase. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Request.is_secure` is `True` for a `wss` connection and for any casing of an encrypted scheme; `request.scheme` and `url_for(_external=True)` are normalised to lowercase per RFC 3986 Sec. 3.1. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP HTTP transport requires and cross-checks `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` on the `2026-07-28` revision. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `serve_stdio` isolates the protocol wire, so handler or subprocess output cannot corrupt the JSON-RPC stream. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCPAuthorizationServer.verifier(resource=...)` enforces RFC 8707 audience binding, refusing a token minted for another server. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A registered OAuth client is refused a grant type it did not register for. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A route exposed as an MCP tool keeps its rate limit; the replayed call reported no caller, so every call got a fresh bucket. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `@rate_limit` is enforced above a `max_requests=`/`window_seconds=` limiter; the tag was dropped in silence, leaving the route unthrottled. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP token minted without a `resource` no longer satisfies a verifier configured with `resource=`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A resuming MCP `GET` checks `Mcp-Session-Id`, so a terminated session cannot replay its buffered payloads. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HTTPBasicCredentials` and `HTTPDigestCredentials` mask the password and the digest response in their `repr`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint-scoped `before_request` runs on every route of its blueprint; a dotted route name skipped it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))

### Added

- `Veloce.instrumentation_hooks` returns the registered instrumentation hooks in run order, the read half `add_instrumentation` lacked. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `URLMap` is exported from `veloce` and is the public name of the class `Veloce.url_map` returns. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `WebSocketState` is exported from `veloce`; it is the declared return type of `WebSocket.application_state` and `.client_state`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Severity` is exported from `veloce`; it is the declared type of `Finding.severity`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `RateLimitState` is exported from `veloce`; a `RateLimitStrategy` implementation names it in `evaluate`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SignalResult` is exported from `veloce`; it is the declared return type of `Signal.send`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.register_auditable(component)` reports a non-middleware component to `veloce check` and `security_audit()`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `unregister_converter(name)` removes a converter added with `register_converter`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCPServer(capabilities=[...])` serves an out-of-tree `Capability`; `MethodHandler` is exported to annotate its handler map. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `TestResponse` is exported from `veloce` and documented; it is what every test-client call returns. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.contrib.mcp` publishes all seven capability classes, not three. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.wait_for_background_tasks()`, and the same on both test clients, waits for spawned background work to finish. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCPServer.capabilities` exposes the capabilities a server was built with. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddlewareBase.wire_cookie_name` exposes the cookie name after the `__Host-` / `__Secure-` prefix. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddleware.bind_secret_key()` settles the signing key from a config before the first request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.middlewares` exposes the registered middleware instances in pipeline order. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddleware.encode_cookie()` / `.decode_cookie()` sign and verify a session cookie outside a request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `InMemorySessionStore` supports `in`, iteration, `expires_at()` and `clear()`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.iter_routes()` returns each route as `(method, path, RouteInfo)`; `app.routes` remains the six-field summary. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CompressionMiddleware` negotiates zstd, brotli and gzip from `Accept-Encoding`; install the `brotli` / `zstd` extras to offer the newer codings. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The `ciso8601` extra accelerates the `{x:datetime}` path converter for values without a numeric offset; what matches is unchanged. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `RateLimitStrategy.lua_script` / `lua_argv`, an opt-in server-side form a backend can run in place of the Python `evaluate`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.http.response.header_pop`, the replacement half of `header_get` / `header_present`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Veloce()` warns when an unrecognised keyword looks like a misspelled parameter (`tittle=` for `title=`), which was previously absorbed into `app.extra` in silence. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce check` reports an MCP endpoint mounted without `auth=` or without `allowed_origins=`, as `mcp-endpoint-unauthenticated` and `mcp-origin-unchecked`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Blueprint` accepts `tags=` and `on_duplicate=`, the two `Router` options it dropped. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `StaticFiles(max_age=...)` sets the cache lifetime, and the handler honours `SEND_FILE_MAX_AGE_DEFAULT` as `send_file` already did. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `GRACEFUL_DRAIN_TIMEOUT` bounds how long shutdown waits for in-flight requests; it was a literal 30 seconds no setting could reach. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Auditable` carries the audit contract for every middleware shape, so a `BaseHTTPMiddleware` can declare `sets_hardening_headers` and contribute findings. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Converter.specificity` declares how restrictive a custom converter is, so it can outrank `str` during route matching. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SecurityScheme.openapi_scheme` publishes a custom authentication scheme in the OpenAPI document like a built-in. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `WEBSOCKET_IDLE_TIMEOUT` closes an idle WebSocket with `1001 Going Away` on both transports; it was read only by the built-in server, and is now a documented config key. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `TestClient(app, loop=...)` drives the app on a loop you supply, which the client never closes. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Middleware.audit` lets any middleware contribute findings to the audit, with a severity that decides whether startup refuses to serve. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Finding`, `AuditContext` and `AuditFailed` carry a severity, a remedy and a stable id; `veloce.audit.run(app)` returns them. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SILENCED_AUDIT_IDS` drops named findings, so an accepted one is turned off without turning the audit off. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Middleware.audit_needs_routes` skips a route-reading check until the route table is final. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SecurityHeadersMiddleware` reports the opt-in headers it is not sending, with the value to pass. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Middleware.sets_hardening_headers` marks a middleware that adds hardening headers, satisfying the audit's headers check. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddlewareBase` is public; subclass it to add a session backend that `security_audit` recognises. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HeaderMismatchError` rejects a modern MCP request whose standard headers disagree with its body. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce check` reports an `exclude_middleware` name that matches no registered middleware. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Signal.doc` records the description given to `Namespace.signal(name, doc=...)`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A second route taking an existing `name=` logs a warning naming both paths. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))

### Changed

- A `tools/call` over the HTTP transport is answered as JSON when the tool cannot send a second message, instead of always framing an SSE stream. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An insufficient-scope `tools/call` on a tool answered as JSON returns `403` with a scope challenge, where a streamed one carries the error in band. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The built-in server's accept queue follows the machine's `somaxconn` rather than asyncio's default of 100. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `make_response` answers a one- or four-element tuple as data; it dropped a four-element tuple's status and headers in silence. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Veloce.make_response`, `veloce.make_response` and dispatch read one response-tuple table, so a tuple cannot answer three ways. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A handler may return `(body, header_list)`; dispatch read the pair list as a status and answered `500`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Blueprint` refuses a dot in its own name or a route's `name`; the dot separates the blueprint from the route in an endpoint. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `make_response(response, status, headers)` applies them instead of returning the response untouched; an omitted status is `None`, not `200`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HTTPBasicCredentials` and `HTTPDigestCredentials` compare by identity, so both are hashable again. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A conditional `GET` for a streamed response answers `304`; an asset past `FileResponse`'s streaming threshold was re-sent in full. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `304` advertises the length the equivalent `200` would carry, or none when it is unknown, instead of `0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `{value:float}` is matched before `{value:decimal}`; float accepts a strict subset, so it is the more restrictive of the pair. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `parse_multipart_form` is no longer re-exported from `veloce.http.datastructures`; import it from `veloce.http`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `exclude_middleware` accepts a middleware class, matched by type so it covers subclasses; a string still matches the resolved name exactly. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `exclude_middleware` raises `TypeError` for an entry that is neither a middleware class nor a name; such an entry previously matched nothing in silence. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `RedisRateLimitBackend` runs a built-in strategy as a Lua script - one round trip instead of three, executed atomically, with no contended-key fallback that could admit requests over the limit. A custom strategy keeps the `WATCH` path unless it declares `lua_script`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `View`, `JSONProvider`, the path-converter base and the MCP registry base refuse a subclass that omits a required method, at definition. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Cache` and `SessionStore` refuse a subclass that omits a required method, at definition rather than on the request that first calls it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A traceback frame from a compiled resolver names its handler and shows its source, instead of a bare `<veloce-resolver>` with no line. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response_model_*` filter flags resolve when the route is registered rather than on every response; assigning one to a `RouteInfo` afterwards no longer takes effect. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A compiled resolver converts `str`, `int` and `float` parameters inline instead of calling the shared coercion helper per parameter. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A response with no background work returns before allocating the task list it used to build and discard. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP JSON-RPC envelope is encoded as protocol on every transport, so a custom `json_provider_class` no longer injects its keys into protocol frames and `JSONIFY_PRETTYPRINT_REGULAR` no longer inflates each SSE frame. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `/openapi.json`, `/docs` and `/redoc` are excluded from the OpenAPI document they serve, so a generated client no longer carries three operations for them. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `instance_path=` must be a rooted path; a relative one resolved against the working directory the process happened to start in. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An env file refuses a value that is not the type its config key declares, naming the key. `DEBUG=flase` read as `False`; an empty value still reads as off. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP `Capability` declares the methods a modern revision retired as `handshake_only_methods`; the server derives what it refuses from those instead of a separate table. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An app-level `url_value_preprocessor` runs before a blueprint's, matching the request hooks; registration order no longer interleaves the two. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.url_value_preprocessors` and `app.url_default_functions` key each blueprint's entries under its dotted name instead of flattening them under `None`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Three deferred imports of Veloce modules are hoisted to module scope and three are documented with the cycle they break. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SecurityHeadersMiddleware` applies its headers with one pass over the response's keys instead of a case-insensitive scan per header, saving 1-4 us per response. Output is unchanged. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `APPLICATION_ROOT`, `MAX_COOKIE_SIZE` and `PERMANENT_SESSION_LIFETIME` are removed from the config defaults and stop startup when set; pass `path=`, `max_cookie_size=` and `permanent_lifetime=` to the session middleware. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddleware` and `ServerSessionMiddleware` take every cookie setting from their constructor; `SESSION_COOKIE_NAME`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY` and `SESSION_COOKIE_SAMESITE` no longer configure them, and setting one stops startup with `AuditFailed`. Pass `secure=True` and the rest as arguments. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SECRET_KEY` remains the one session setting read from the app; a middleware given no `secret_key=` still signs with it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionMiddlewareBase.cookie_lifetime` replaces the private `_cookie_lifetime`; a subclass calling the old name must rename it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `security_audit` asks each registered middleware for its findings instead of naming middleware classes, so a middleware written outside Veloce is audited like a built-in. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Startup runs the full audit and refuses to serve on an `error` finding, raising `AuditFailed`; `RateLimitMiddleware` raised `ValueError` for the same case, which `AuditFailed` still is. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce check` labels each line with its severity and exits 0 when only `info` findings are present. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A nested blueprint's hooks and URL processors run only on that blueprint's own routes, not on every route under its parent. Register a guard on the parent blueprint to keep app-wide coverage. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `ServerSessionMiddleware` honours `session.permanent`, so a permanent session's cookie and store entry both use `permanent_lifetime=`. Sessions that previously expired at the 14-day default now live as long as that argument says. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `security_audit()` warns about a non-`Secure` session cookie for any session middleware, so an app using `ServerSessionMiddleware` may see a warning `veloce check` did not previously report. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Malformed JSON in a body model follows the same policy as `request.json()`: a `400` with a stable message, and the decoder's reason only under `JSON_ERRORS_VERBOSE` or debug. It was a `422` with a generic message, so the opt-in did nothing there. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `run(access_log=True)` writes a per-request access line; it previously printed only the startup banner. The ASGI path is unchanged, where the server already logs. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `bool` query, path, header or cookie parameter accepts Pydantic's spellings (`on`/`off`, `t`/`f`, `y`/`n` as well as `true`/`false`, `1`/`0`, `yes`/`no`) and refuses anything else with a `422`; it previously read every unrecognised value, including `on` and any typo, as `False`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `log_exception` takes an optional `request=` and names it in the record. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Query strings and urlencoded bodies carrying no percent-escape skip per-field decoding. Measured on one Windows desktop: 20.9 vs 26.0 us for a three-parameter query read, 35.3 vs 43.4 us for a five-field form POST; an escaped value is unchanged. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.__version__` reads the package metadata on first access instead of at import. Measured on one Windows desktop: about 14 ms off a ~342 ms cold start. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A cookie value is percent-decoded only when it carries an escape or a quote. Measured on one Windows desktop: 1.84 vs 3.03 us for a four-cookie header. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Content-Type` parsing skips the parameter walk when the header declares no parameters. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The `MAX_CONTENT_LENGTH` header scan matches the name as ASGI mandates it rather than lowercasing every header. A server that sends a differently-cased `Content-Length` loses the early rejection, not the limit. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `TestClient` builds a `SelectorEventLoop` on Windows; pass `loop=asyncio.ProactorEventLoop()` for a handler that spawns a subprocess. Measured on one Windows desktop: 73.0 vs 93.6 us per request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An ASGI WebSocket message is read and written one coroutine frame deep when no timeout is set. Measured on one Windows desktop: 1.34 vs 2.02 us per echo round. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP `list[T]` tool argument refuses a non-array and a wrong-typed member instead of wrapping or passing it through; send the array the published schema declares. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `from_prefixed_env` coerces a value to its key's declared type and refuses one it cannot convert. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Blueprint.errorhandler` raises `TypeError` for a key that is neither a status code nor an exception class. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP transport trims a bearer token as RFC 7235 allows; a token carrying other whitespace is now refused. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HttpSessionStore.resolve` no longer scans every live session, so MCP request cost stays flat under load. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `@app.middleware("http")` raises `TypeError` for options it cannot honour instead of dropping them. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `MethodView` verb method declaring a parameter marker or `Depends()` raises `TypeError` at class definition. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Config.from_mapping` raises `TypeError` for a non-uppercase keyword argument instead of dropping it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Route registration no longer computes a dependency-grouping map that nothing read. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP server-to-client request is refused when the client advertised the capability as `false`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `register_blueprint` raises `ValueError` for a different blueprint under a name already registered; give one a different name. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `InMemorySessionStore` supports `len()`, so an empty store is now falsy; test `store is not None` to mean a store is configured. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `jsonable_encoder` applies `include` as a key whitelist at every depth; list a nesting key or its branch is dropped. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))

### Removed

- `Veloce.use_secure_defaults()`; register `SecurityHeadersMiddleware(hsts_max_age=31536000)` and pass `secure=True` to the session middleware. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.routing.params` is removed; import the markers from `veloce` or `veloce.routing`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))

### Fixed

- A streamed file sends no more than the `Content-Length` it declared when the file grows mid-response. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `list`-typed `Header()` or `Cookie()` resolves on a websocket route instead of closing the handshake `1011`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `/openapi.json` gives a request schema its own component when a nested model is also returned by another route. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The derived-model cache for `response_model_include` / `_exclude` keys on the model, not its address. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP HTTP transport publishes the session from its SSE reply path, so a conformant client's handshake is recorded. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `@rate_limit`-tagged route's backend honours the middleware's `max_keys`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP stdio ordering wait no longer absorbs a cancel delivered to its own task. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP server-to-client request whose emit fails leaves no entry in the pending correlation table. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A grouped parameter's schema walk reports failure instead of silently publishing the field without its `ge` / `le` / `title`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `jsonable_encoder` applies `exclude` below a model, not only to its own fields. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Query(default=[])` and other mutable marker defaults are copied per request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `client.session_transaction()` applies the middleware's own age ceiling, so a cookie a request would refuse no longer loads. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A status-code error handler taking `(request, exc)` is called correctly on the unhandled-exception and `405` paths; it raised `TypeError` out of dispatch. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SessionAuth` describes itself in the OpenAPI document, so a session-guarded route declares a security requirement instead of publishing as open. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The legacy MCP SSE transport sends its reconnect hint on the first frame; no frame carried `retry` before. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A header parameter containing a backslash is quoted and escaped, so a `Content-Disposition` filename ending in one no longer emits an unterminated quoted-string. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The exported `http_exception_handler` renders the same body as the default error path: an empty detail is not replaced with `"Error"`, and a body-limit refusal keeps its `limit`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `AcceptHeader.best_match` honours an explicit `q=0` on a non-MIME header, so `Accept-Encoding: gzip;q=0, *` no longer selects `gzip`. Affects `request.accept_encodings` and `request.accept_languages`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.make_response` returns a `Response` argument unchanged; it previously JSON-encoded the object into its own repr. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.make_response` types a `bytes` body as `text/html`, matching `Veloce.make_response` and dispatch. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The startup banner prints the installed framework version; it printed the app's `version=` argument, defaulting to `0.1.0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `TestClient` constructor error surfaces instead of being buried by an `AttributeError` from the finaliser. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The declared `Content-Length` check is skipped for methods that carry no body; the received-length cap is unchanged and still refuses an over-limit body on any method. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `WebSocket.send_json(mode="text")` frames the encoded payload directly on the built-in server instead of decoding and re-encoding it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CompressionMiddleware` memoises the negotiated coding per `Accept-Encoding` value, bounded at 256 entries. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Generated resolver source listings registered for tracebacks are capped, bounding a process that registers routes over its lifetime. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An after-request hook or error handler that cannot be weakly referenced - a slotted callable, a method descriptor - runs instead of answering `500`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A validation error reports an array index in `loc` as an integer on every body path, matching the published `ValidationError` schema. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `jsonable_encoder` applies `exclude_unset` and `exclude_defaults` to a nested model, not only to one passed in directly. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `jsonable_encoder` applies `exclude_none` to an arbitrary object's attributes, matching every other branch. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response_model_include` / `response_model_exclude` shape the OpenAPI response schema, so the document names exactly the fields the route sends. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Response.check_preconditions` enforces `If-Unmodified-Since` alongside `If-Match`, in RFC 9110 precedence; a date-based precondition was previously ignored. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An exception handler declared `def handler(**kwargs)` receives `request` and `exc`; it was called with an empty mapping. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A dataclass or `TypedDict` return annotation declares a response contract, so the return is filtered rather than served whole. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response_model=<dataclass>` filters a value of a different dataclass instead of raising; it previously answered `500`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A tuple return that is not `(body, status[, headers])` reads the same with and without a `response_class`; the `response_class` path took the first element and discarded the rest. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A parameter declared on a dependency is published in the OpenAPI document; it was enforced with a `422` but absent from the schema. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A tool declared with `@app.mcp_tool` renders its result in the app's JSON dialect, as a route-exposed tool already did; the two disagreed and the route-backed one only matched by accident. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CORSMiddleware` keeps an `Access-Control-Expose-Headers` entry another middleware contributed under any casing; it checked two spellings and silently discarded the rest. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Replacing `Allow` or `Content-Length` clears the existing header whatever casing it was stored under, so a response cannot carry two. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.openapi_version` is emitted in the generated document; it was documented as the spec version the document carries and was read nowhere. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The OpenAPI document and the MCP `initialize` result report the same application title; the two builders carried different fallback defaults. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A pre-dispatch refusal (the 413 on both transports, and the ASGI 400) is encoded in the app's JSON dialect. These ran before the app contextvar was bound, so the dialect appeared only when an earlier request had left it set on the same task. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.run()` answers a `MAX_CONTENT_LENGTH` refusal with the same JSON body as the ASGI path; it sent `Content Too Large` as untyped text, so a client parsing the documented error shape failed on that transport only. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `title` and `version` must be non-empty strings; a non-string produced an invalid OpenAPI document and a 500 on `/docs`, and `validate_openapi=True` did not catch it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An `exception_handlers=` key that is not an int status code or an exception class raises `TypeError`. A string key was stored in a table matched by MRO walk, so the handler never fired. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `docs_url=""` and `redoc_url=""` disable that page instead of mounting it at the site root, and two documentation pages sharing a path are refused at construction rather than on a later request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A text `response_class` given a `dict` or `list` raises `TypeError` naming the class and the remedy, instead of `AttributeError: 'dict' object has no attribute 'encode'`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `/docs` and `/redoc` point at the schema path the app actually serves, including `prefix=` and `root_path`. Both rendered empty on a prefixed app, and ReDoc had no way to override it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `mount_mcp(transport="sse", auth=...)` serves the RFC 9728 protected-resource metadata its `401` challenge points at; the route was registered by the HTTP transport alone. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP stdio transport encodes a reply with the same fallback the HTTP path uses, and answers `-32603` rather than writing nothing when a value cannot be serialised. A `Decimal` in `ctx.result_meta` hung the client. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP request naming a handshake-era revision in `_meta` is served as handshake-era by both the transport and the core, so it no longer skips the standard-header cross-check while being answered in the modern envelope. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A mounted sub-app that cannot serve a request raises instead of silently falling through to the next mount with the body already drained. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint `url_value_preprocessor` no longer runs on every request nor costs every route in the app its straight-line dispatch. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Veloce.process_response` runs the dispatch path, so a hook declaring only `response` no longer raises, a non-`Response` return no longer replaces the response, and `after_this_request` callbacks run. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Router(tags=[...])` copies the list instead of appending route tags to the caller's own. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `TestClient.cookies` and `TestResponse.cookies` report the decoded value the handler receives, not the percent-encoded wire form. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A route whose `response_model=` disagrees with its return annotation fails `veloce check`; it was printed and the command exited 0. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Response-contract findings carry ids, so `SILENCED_AUDIT_IDS` reaches them, and are reported by severity rather than only under `debug`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCP_CALL_TIMEOUT`, `MCP_ENFORCE_LIFECYCLE`, `MCP_RESOURCE_SUBSCRIPTIONS` and `EVENT_LOOP_WATCHDOG` are declared config keys, so an env-file value gets the right type; `MCP_CALL_TIMEOUT=5` reached `asyncio.wait_for` as a string and broke every tool call. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The configuration guide documents every shipped key, and a test fails when the two drift apart. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.mount()` accepts any handler exposing `prefix` and `handle(request)`, not only a `StaticFiles`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A subdomain route no longer matches an IP-literal host, where the router and `request.subdomain` disagreed. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An instrumentation hook marked `is_access_log` suppresses the built-in access log, so `run(access_log=True)` does not log twice. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `TestClient` restores the app's setup lock on close instead of leaving it disabled. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `session_transaction()` seeds the cookie under the name the middleware reads, so a `cookie_prefix=` session is found instead of silently running anonymous. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `session_transaction()` works before the first request when the signing key comes from `app.config`, and explains why a server-side backend cannot be seeded. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Veloce(root_path=...)` reaches `request.root_path`, `script_root`, external `url_for` and slash redirects; it was stored and read by nothing. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint mounted at two prefixes runs its hooks and URL processors once per request, not once per mount. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Silencing `ratelimit-overrides-unknown` no longer turns a startup refusal into a 500 on every request; the request path reports and the audit decides. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `security_audit` walks dispatch-shape middleware, ASGI middleware classes and static handlers, not only `Middleware` instances; a correctly hardened app was reported as unhardened. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `StaticFiles` directory that does not exist is reported by `veloce check`; it previously only warned at construction. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `security_audit` no longer warns that `SECRET_KEY` is unset for a session middleware constructed with its own `secret_key=`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A session middleware with no signing key from either source refuses startup instead of raising on the first request. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `JSONResponse` and a bare mapping yielded to `EventSourceResponse` honour `JSON_SORT_KEYS` and a custom JSON provider; both encoded directly and missed the app's dialect. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `date`, `datetime`, `time`, `timedelta`, `decimal`, `any(...)` or custom converter outranks `str` whatever the declaration order; which route answered depended on which was declared first. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A route guarded by a custom `SecurityScheme` is published with its security requirement; the document asserted the route was open. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A scheme that cannot describe itself warns during the schema build instead of silently publishing the route as unauthenticated. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `from_env_file` gives a value the type its config key is read as, so `MAX_CONTENT_LENGTH=1000` no longer raises `TypeError` on every request with a body. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `DEBUG=false`, `JSON_SORT_KEYS=false` and `TCP_KEEPALIVE=false` in an env file read as off; a non-empty string was truthy, so they read as on. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `SILENCED_AUDIT_IDS` from an env file splits on commas; left a string, a membership test matched single characters. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint's `before_request`, `after_request` and `teardown_request` run on routes of a nested blueprint that declares none of its own; a guard on a parent blueprint was skipped there. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `security_audit` no longer warns about a session middleware constructed with an explicit `secure=True`; it read only `SESSION_COOKIE_SECURE`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The `Connection` header states what the built-in server actually did: an HTTP/1.0 request, one asking for `Connection: close`, and a native SSE stream were all answered `keep-alive` on a socket the server then closed. `EventSourceResponse` no longer sets `Connection` as a response header. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `StreamingResponse` with a bodiless status (`204`, `205`, `304`) sends no chunks and advertises no `Transfer-Encoding`, which RFC 9112 Sec. 6.1 forbids there; it previously desynchronised keep-alive connections. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Assigning `response.body` refreshes `Content-Length`, as `set_data` already did; a middleware rewriting a body advertised the previous length. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `HEAD` on an `EventSourceResponse` advertises the chunked framing its `GET` uses instead of `Content-Length: 0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Every `Response` header accessor reads the header under any casing; `vary`, `allow`, `cache_control`, `date`, `location`, `age` and seven others saw only the canonical spelling. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response.expires = None` and `last_modified = None` remove the header whatever casing it was stored under; a third spelling survived while the getter reported the clear had worked. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The built-in server percent-decodes the request path, as an ASGI server does, so `/items/a%20b` binds `"a b"` on both; it bound the raw `"a%20b"`, and a handler saw a different value depending only on how the app was served. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce.make_response` unpacks a `(body, status)` / `(body, status, headers)` tuple as `app.make_response` and a handler return already did; it JSON-encoded the whole tuple, status code included, into a `200` body. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `app.make_response` coerces any value rather than raising `TypeError` on `123` or `None`, matching what a handler returning the same value is answered. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A file above the 64 KiB inline threshold is streamed off disk instead of read whole, so resident memory no longer scales with file size times concurrent downloads. It still advertises `Content-Length`; `FileResponse.body` is empty for such a response and the bytes arrive as chunks. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `stream=True` route no longer retains the body it streams: a 32 MiB upload left 33.6 MB resident after the handler had consumed it, so the flag bounded nothing. A streamed body is consume-once, and `request.body()` afterwards reports empty. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `stream=True` route keeps streaming when its app is mounted; the mounted dispatch buffered the whole body first, so the flag silently stopped applying under composition. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `samesite` value is normalised once, inside the `Set-Cookie` serialiser; a whitespace-only value made the cookie-backed session raise on every response while the server-side one silently omitted the attribute. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `url_for` refuses a `/` in a value bound to a segment-bounded path converter instead of returning a URL its own router answers `404` for; a `path` placeholder still accepts slashes. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `request.subdomain` reads the host through the framework's shared reader, so an IPv6 literal is not split on its first colon, and an IP literal - v4 or v6 - reports no subdomain rather than an address label. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A `{v:float}` placeholder on a regex-fallback route accepts `+1.5`, `.5` and `5.`, which the radix tree already did; the fallback's pattern was stricter than the converter and refused them first. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint route declared `strict_slashes=False` keeps it through registration; the same route reached through `include_router` kept it, so one declaration behaved two ways. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A plain mutable parameter default (`tags: list = []`) is fresh on every MCP tool call, as it already was on every HTTP request; one call's mutations reached the next. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `WebSocket.close()` clamps its reason to the RFC 6455 control-frame budget on both transports and never puts a reserved close code (`1005`, `1006`, `1015`) on the wire; an over-long reason previously reached the peer as an abnormal `1006` under an ASGI server. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A refused WebSocket handshake applies the host/Origin allow-lists before matching the route on both transports, so a refused origin cannot learn which paths exist; the built-in server matched first and answered `404` for an unknown one. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An ASGI WebSocket handshake for an unregistered path answers `404` where the server advertises the `websocket.http.response` extension, instead of a `1008` close indistinguishable from a policy refusal. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An over-`MAX_CONTENT_LENGTH` rejection runs the response phase and states one message on every route kind; the ASGI refusal was written before a `Request` existed, so it carried none of the app's response headers and worded itself differently from a `stream=True` route's. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MAX_FORM_PARTS = None` lifts the field cap for a multipart body as it already did for a urlencoded one; multipart kept the built-in 1000-part limit whatever the setting said. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP tool argument declared inside a `Depends` is held to the type the tool's `inputSchema` publishes, as a top-level argument already was; `"yes"`, `"1"` and `1` were read as `true` for a declared `bool` behind a dependency. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCPContext.list_resources` and `list_prompts` omit what the connection hid with `hide()`, matching what `resources/list` and `prompts/list` report; they are unpaged, since a handler cannot ask again for the next page. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A closed legacy MCP SSE stream reclaims its session, so an unsettled task and its runner no longer outlive the connection for the process lifetime. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The legacy MCP SSE transport distinguishes an unreadable body (`-32700`) from a readable one of the wrong shape (`-32600`), as the other two transports do; it answered `-32603` for both. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP request naming a handshake-era revision in `_meta` is served instead of refused for a header that revision never defined. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `describe_tools` advertises the modern tool shape to a modern client; under `tool_search` it is the only definition such a client sees. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `tasks/cancel` and the task status notification spell the duration fields the way the caller's revision does, matching creation and polling. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A WebSocket handler's exception is reported on the built-in server; a peer that left cancelled the close handshake and the failure was lost. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An unhandled exception is logged with its traceback and the failing request instead of vanishing into a generic `500`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A quoted `Content-Type` parameter containing `;` is no longer cut short: `profile="a;b"` was read as `"a`. The same applies to the `Accept` media-range key. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A quoted header parameter written with whitespace before the opening quote (`filename = "r.pdf"`) no longer keeps that space in its value. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The deployment guide's deprecation section named the wrong warning category, so the command it gave caught nothing. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce --version` reports the same fallback as `veloce.__version__` when the package metadata cannot be read; it claimed `0.3.0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `stream=True` is honored on a blueprint route and on an included router's route. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A blueprint route keeps its MCP resource mime type, size, annotations and `_meta`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Capabilities are advertised per protocol revision, so none is offered and then refused. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `tools/list` omits `execution` for a modern client, whose revision removed the field. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `server/discover` is marked private rather than publicly cacheable; its answer varies by caller. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `subscriptions/listen` works on the default HTTP deployment instead of requiring a persistent session. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Query(group=True)` and its header and cookie forms appear in the OpenAPI document and in MCP tool schemas. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP tool call binds a grouped model instead of failing with an internal error. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- A grouped field's declared constraints reach both published contracts instead of only the runtime. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `g` keeps a value written by a sync handler or an offloaded dependency. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `MCPContext.result_meta` written by a sync tool reaches the client. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Response.add_vary` merges an existing `Vary` stored under any header casing instead of dropping it. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Clearing `location`, `date`, `age`, and six sibling response headers works under any stored casing. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `GZipMiddleware` honours a `Q=0` refusal, not only the lower-case spelling. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The MCP transport does not send an SSE stream to a client that refused it with `q=0`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `RedisRateLimitBackend` falls back to optimistic locking on a server with scripting disabled. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Dynamic client registration stores the requested `grant_types` and echoes what it stored. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `HTTPBearer(scheme_name=...)` publishes that scheme in the OpenAPI document, not a fixed `bearer`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `handle_http_exception` renders the same body as the request cycle for an exception with an empty detail. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `response_model=Sequence[Model]`, `tuple[Model, ...]` and `set[Model]` document an array of refs. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- The `HTTPValidationError` schema declares the `status_code` every 422 response carries. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `Body()` on a model parameter validates it, instead of passing the raw decoded body to the handler. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- An MCP tool schema publishes a parameter marker's constraints, matching the OpenAPI document. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `CORSMiddleware`'s usage example lists `allow_headers`, which credentials require. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Mounting an MCP transport twice no longer leaves the first mount unreachable by `url_for`. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- Fix the order of grouped lifespan-teardown failures; an expanded exception group's members were reported backwards. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `request.is_disconnected()` reports `True` on the built-in server when a client vanishes mid-body; the disconnect was signalled as an ordinary end-of-body. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))
- `veloce mcp run --transport http` says which server it fell back to when uvicorn is absent, as `veloce run` already did. ([#289](https://github.com/Lokesh-Tallapaneni/veloce/pull/289))

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
