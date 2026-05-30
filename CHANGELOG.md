# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Request bodies now stream. The raw HTTP/1.1 server dispatches a handler as
soon as the request line and headers are parsed and feeds the body in as it
arrives over a bounded, backpressured queue, instead of buffering the whole
body in memory before dispatch. Peak per-connection body memory is now the
queue bound rather than the full upload, so many concurrent large uploads no
longer scale memory with body size.

### Added

- The built-in HTTP/1.1 server's keep-alive and slowloris read timeouts are
  now configurable through `app.config`: `KEEP_ALIVE_TIMEOUT` (idle-connection
  timeout) and `REQUEST_TIMEOUT` (per-request read budget). Defaults are
  unchanged at 75 and 30 seconds respectively.
- The built-in HTTP/1.1 server now honours `Expect: 100-continue`: a request
  carrying that header is answered with an interim `100 Continue` once its
  headers are parsed, clearing the client to send the body. The interim is
  suppressed for HTTP/1.0 clients and when the declared `Content-Length`
  already exceeds `MAX_CONTENT_LENGTH` (the request is rejected with `413`
  instead).
- **Hybrid routing: radix fast path with a regex fallback.** Routes the radix
  tree cannot express now match through a compiled-regex fallback consulted only
  on a tree miss, so patterns such as a parameter sharing a segment with static
  text (`/v{version:int}/api`), multiple parameters in one segment
  (`/files/{name}.{ext}`), a raw regex converter (`/items/{id:[0-9]+}`), or a
  greedy `:path` converter followed by a suffix (`/{p:path}/edit`) are now
  supported. Classification happens once at registration; the radix fast path is
  unchanged and pays nothing when no regex route is registered — the tree always
  wins over the fallback. Regex routes participate in `url_for`, allowed-method
  reporting (405/OPTIONS), `include_router` merging (with name prefixing), and
  OpenAPI schema generation (exposed with an OpenAPI-style path, e.g.
  `/items/{id}`).
- **Optional gunicorn worker (`veloce.workers.VeloceWorker`).** An advanced,
  POSIX-only alternative to running under uvicorn: gunicorn manages the process
  pool while each worker drives Veloce's own `HttpProtocol` directly on an
  asyncio event loop, with no uvicorn or ASGI shim in the request path. gunicorn
  is an optional dependency installed via the new `gunicorn` extra
  (`pip install veloceframework[gunicorn]`); importing Veloce never requires it,
  and the worker raises a clear `ImportError` with an install hint if
  instantiated without gunicorn present. Run with
  `gunicorn your_module:app -k veloce.workers.VeloceWorker`. uvicorn remains the
  recommended production default. See the Deployment guide.

### Changed

- Parameter-only handlers (the request plus scalar path/query parameters, with
  no dependencies) now resolve through a straight-line resolver generated and
  compiled once on first dispatch and cached on the route's plan, replacing the
  per-request slot-dispatch loop. Measured ~10–12% faster dispatch on param-heavy
  routes without dependency injection; routes using `Depends`, body models, list
  params, or websocket/background/response injection are unchanged — they use the
  existing resolver.
- The compiled resolver now also handles synchronous parameter markers —
  `Query()`, `Path()`, `Header()`, and `Cookie()` (including their list-typed
  forms) — inlining the source lookup and coercion the per-request interpreter
  performed, plus the `validate()` constraint check for scalar markers.
  Behaviour matches the interpreter exactly, including that list-typed markers
  collect their values without running per-item `validate()`. Measured ~16–22%
  faster dispatch on routes whose parameters are these markers. `Body()`,
  `Form()`, and `File()` markers continue to use the interpreter, since their
  source is read asynchronously and cannot be reached from the compiled
  function.
- **BREAKING — request body access is now asynchronous.** To stream a body the
  framework can no longer hand the handler a fully-formed bytes attribute, so
  the body accessors are now awaitables:
  - `request.body` (attribute) → `await request.body()` (method).
  - `request.text` (property) → `await request.text()`.
  - `request.get_data(...)` is now `await request.get_data(...)`.
  - `request.json()` and `request.form()` were already `async` — no change for
    callers that already `await` them.
  - `request.data` remains a **synchronous** property for the buffered case,
    but raises `RuntimeError` if accessed while the body is still streaming and
    has not been drained yet; use `await request.body()` on the async path.
  - `request.get_json()` remains synchronous and works once the body is
    buffered; it raises a clear error if the body has not been read yet.

  Migration: add `await` to `request.body`, `request.text`, and
  `request.get_data(...)` call sites, and make the enclosing handler/util
  `async`. Code that already used `await request.json()` /
  `await request.form()` needs no change.

- **`async for chunk in request.stream()` is now true streaming on the raw
  HTTP/1.1 server.** Chunks are yielded as they arrive off the socket rather
  than sliced from an already-buffered body, so a handler that only iterates
  `stream()` (e.g. writing an upload straight to disk) processes an arbitrarily
  large body with bounded memory. The in-memory ASGI and `TestClient` paths
  pre-fill the body, so `stream()` there yields the complete body as before.

- **`MAX_CONTENT_LENGTH` is enforced against the streamed running total** (and
  the declared `Content-Length`) and still returns `413` — now rejecting an
  over-large upload mid-stream instead of only after the whole body is
  buffered.

### Fixed

- OpenAPI schemas for non-body parameters (query, path, header, cookie) and
  `Form()` fields now match what the request resolver can actually deliver.
  These values arrive over the wire as raw strings, so a parameter annotated
  with a Pydantic model — or a `list`/`dict`/`set` of one — documents
  `{"type": "string"}` rather than a `$ref`/object schema, and the resolver
  now parses that string as a JSON document into the model
  (`?tag={"name":"x"}` for `tag: Tag = Query()`, repeated JSON-object values
  for `tags: list[Tag] = Form()`), so a value matching the documented string
  schema resolves rather than returning a 422. A multi-member union is
  documented by the branch a string value can actually reach under Pydantic's
  smart coercion: a union that includes `str` (`int | str`,
  `Optional[int | str]`) documents `{"type": "string"}`, since string input
  always lands on the `str` member; a union with no string-accepting member
  (`int | float`, `UUID | int`, `date | datetime`) documents an `anyOf` over
  its members, since the resolver genuinely resolves the string to one of
  those typed branches. Pydantic models carried as a structured JSON body
  belong in `requestBody`, where they are still resolved to a `$ref`.
- **Hybrid router: unknown converters in regex-routed paths now raise at
  registration.** A bare-word converter typo in a path that forces regex
  routing — `/v{version:bogus}/api`, or in a later segment such as
  `/v{version:int}/{id:bogus}` — previously slipped through as literal regex and
  matched the text `bogus`. Every bare-word spec across all segments of a regex
  route is now validated against the converter set and raises `unknown path
  converter` at registration, the same as whole-segment placeholders.
- **Hybrid router: registered custom converters in regex-routed segments now
  raise at registration.** A custom converter — `register_converter("slug", …)`
  — used in a segment that forces regex routing (`/v{name:slug}/api`) previously
  miscompiled into a regex matching the literal text `slug`, so `/vslug/api`
  matched while `/vabc/api` did not. A custom converter's `match()` has no regex
  representation, so such routes now raise at registration instead of silently
  misbehaving. Custom converters spanning a whole segment (`/posts/{name:slug}`)
  remain radix routes and are unaffected.
- **Hybrid router: regex-route parameters are now coerced like radix-route
  parameters.** A built-in converter on a regex route (`/v{n:int}/x`,
  `:float`, `:uuid`, `:any(...)`) now yields the coerced Python value (e.g.
  `3`, not `"3"`) instead of the raw matched string. Bare `{name}` and raw
  regex (`{id:[0-9]+}`) groups remain strings.
- **Hybrid router: regex-route converters now enforce the same rejection
  semantics as radix routes.** A built-in converter's guards — notably the
  `:int` digit cap — are now applied to regex-route matches: when the converter
  rejects its group, the route is treated as a full miss (404) and the next
  route is tried, instead of leaking the raw string through to the handler.
  Previously `/v{n:int}/x` matched an over-long value (e.g. a 21-digit number)
  and passed it as `str`, while the equivalent radix route `/x/{n:int}`
  correctly rejected it. Allowed-method reporting honors the same rejection, so
  an over-long `:int` yields a 404 rather than a 405.
- **Hybrid router: raw regex converter specs may now contain braces.** Patterns
  with brace quantifiers — `/x/{id:[0-9]{2}}` or `/x/{id:\d{2}}` — are parsed
  balance-aware and compile correctly instead of raising a group-name error.
  OpenAPI path reduction and `url_for` handle these specs too.
- **Hybrid router: `strict_slashes=False` is honored on regex routes.** A regex
  route registered with `strict_slashes=False` now accepts the missing or extra
  trailing slash in both `match()` and allowed-method reporting, matching tree
  routes.
- **OpenAPI: a tree route shadows an overlapping regex handler in the schema.**
  When a radix route and a regex route reduce to the same OpenAPI path and
  method, the schema now describes the tree handler — the dispatch winner —
  rather than the regex handler that never runs for that path.
- **gunicorn worker (`veloce.workers.VeloceWorker`) now honours TLS.** When
  gunicorn is started with `--certfile`/`--keyfile` (`cfg.is_ssl`), the worker
  builds a server SSL context from gunicorn's config and passes it to
  `create_server`, instead of handing the bound sockets to asyncio with no TLS
  and silently serving cleartext. If the certificate chain is missing or cannot
  be loaded the worker fails fast with a `RuntimeError` rather than downgrading
  an HTTPS deployment to cleartext. The default context is routed through
  gunicorn's documented `ssl_context(config, default_ssl_context_factory)` hook,
  so a configured TLS customization (minimum TLS version, mTLS tweaks, ciphers)
  is honoured instead of ignored.
- **gunicorn worker stops when the master dies.** The heartbeat loop now also
  checks arbiter liveness (the worker's parent pid changing after a fork-reparent)
  and shuts the worker down instead of leaving it orphaned if the gunicorn master
  goes away.
- **gunicorn worker honours `--max-requests`.** The worker now counts completed
  requests and clears `alive` once the count reaches gunicorn's `max_requests`
  (with any `max_requests_jitter` already folded in by gunicorn), so worker
  recycling works as documented; previously the counter was never incremented and
  recycling never fired. Counting is driven by a new optional
  `HttpProtocol.on_request_complete` hook that is unset (and free) on the
  uvicorn / `Veloce.run()` path. The per-connection serve loop now also consults
  an optional `HttpProtocol.should_keep_serving` predicate at each request
  boundary, so once `max_requests` clears `alive` a connection with queued or
  pipelined requests stops at the boundary and closes instead of draining the
  rest of its queue past the limit before the worker restarts.
- **gunicorn worker no longer leaks a listener on a partial multi-bind failure.**
  When gunicorn hands the worker more than one bound socket, the worker creates
  one asyncio server per socket. If a later bind failed, an already-created
  listener stayed live while the worker proceeded into shutdown. The worker now
  closes every listener created so far before re-raising, so a failed startup
  leaves no live listener behind.

### Internal

- The raw protocol serves pipelined requests through a per-connection FIFO loop
  (responses preserved in request order), drains-and-discards any body a handler
  leaves unread so keep-alive connections cannot be corrupted by leftover bytes,
  and pauses socket reads when the body queue fills, resuming as the handler
  drains it.

## [0.1.4] - 2026-05-25

Post-v0.1.3 audit batch: verified findings from a per-file framework
audit (validated by a second-pass agent with cross-file grep proofs;
false positives discarded). Four security fixes, four correctness
fixes, six performance wins, eight duplication consolidations, and
API surface cleanup.

### Security

- **Multipart UTF-8 decode is now strict.** Header fields and form
  values whose bytes are not valid UTF-8 raise `BadRequest` (400)
  instead of silently passing through `errors="replace"` and producing
  collidable field-name strings. The fuzz suite's
  `test_fuzz_multipart_corrupted_valid_body_never_crash` allowlist
  expanded to permit `BadRequest` alongside the existing
  `RequestEntityTooLarge` DoS-cap rejection — both are controlled
  rejections, neither is a crash.
- **`HTTPBasic` realm now URL-encoded in `WWW-Authenticate`.** Matches
  the existing `HTTPDigest` behaviour. A realm containing `"`, `\r`,
  `\n`, or `\` no longer produces a malformed challenge header.
- **`HTTPBasic` catches only the exceptions `b64decode`/`decode` can
  raise** — `binascii.Error`, `ValueError`, `UnicodeDecodeError` —
  instead of bare `except Exception` that would have masked unrelated
  bugs as 401s.
- **`SecurityHeadersMiddleware` `hsts_include_subdomains` default is
  now `False`.** Casually enabling HSTS on a multi-subdomain host
  should not silently pin every subdomain. Explicit opt-in still
  works (`hsts_include_subdomains=True`).

### Changed

- `jsonable_encoder` no longer recurses infinitely on a self-referential
  object graph; it raises `ValueError("Circular reference detected...")`
  instead. Internal `_seen: set[int]` parameter tracks visited container
  IDs; leaf-only call graphs allocate no extra state.
- `jsonable_encoder` leaf scalars now dispatch through a `type ->
  encoder` dict (`str`, `int`, `float`, `bool`, `None`, `UUID`,
  `Decimal`, `datetime`/`date`/`time`/`timedelta`) instead of a 14-deep
  `isinstance` cascade. Subclasses fall through to the existing
  isinstance arms so behaviour is unchanged.
- `_dispatch_request` merges injected-response Set-Cookie headers via a
  new private `Response._append_set_cookie_header(raw_value)` helper
  instead of the inline `+ "\r\nSet-Cookie: " +` concatenation. The
  same helper is now what `Response.set_cookie()` calls internally,
  giving the codebase one canonical home for the Q44 multi-cookie
  join format.
- `Response.add_vary` delegates dedup to `HeaderSet` instead of
  re-implementing the case-insensitive ordered-set logic inline.
- `signing.py` module docstring now correctly describes the nested
  HMAC algorithm (`HMAC-SHA256(HMAC-SHA256(secret, salt), payload.ts)`)
  the code has always implemented, rather than the previous misleading
  `HMAC-SHA256(secret + salt, payload.ts)` text.
- `Router.url_for` regex compiled once at module top
  (`_URL_FOR_PARAM_RE`) instead of re-compiled on every call.
- Conditional request properties — `access_control_request_headers`,
  `if_modified_since`, `if_unmodified_since`, `if_match`, `if_none_match`,
  `if_range` — now cache their parsed value on `Request` slots so the
  five header re-parses per repeated access become one.
- `contrib/openapi.py` schema generation memoizes per-handler
  `inspect.signature` + `get_type_hints` via a
  `WeakKeyDictionary` (`_handler_intro`), so the four call sites that
  used to each re-introspect every route now share a single per-handler
  read.
- `contrib/templating.py::_sync_app_jinja_helpers` memoizes per
  `(env, app, filter/global/test counts)`; rendering a template no
  longer redoes the three loops + globals copy on every render.
- `middleware/compression.py` wraps the gzip executor offload in
  `contextvars.copy_context().run(...)` so any future ContextVar read
  inside the compressor sees request-scoped values. Matches the pattern
  already in `_call_handler` and the per-request executor sites in
  `app.py`.
- `middleware/security.py::TrustedHostMiddleware` now delegates its
  Host-header port stripping to the shared `_extract_host` helper in
  `_internal.py` instead of the inline IPv6-aware branches that
  duplicated it.
- `middleware/sessions.py::ServerSessionMiddleware._clear_session_cookie`
  centralises the three identical `response.delete_cookie(...)` calls
  for the revocation paths.
- `security/api_key.py` extracts `_APIKeyBase`; `APIKeyHeader`,
  `APIKeyQuery`, `APIKeyCookie` now share `__init__` + `__call__` and
  differ only in a `_source_attr` class var. Public surface unchanged.
- `cli.py` introduces `_require_app_attr(app, attr, hint)`; the four
  `if not hasattr(app, ...)` guards in `_cmd_shell` / `_cmd_custom` /
  `_cmd_routes` / `_cmd_check` now share one error-message shape.
- `testclient.py` extracts module-level `_build_request_headers` and
  `_apply_set_cookie_to_jar`; `TestClient` and `AsyncTestClient` both
  call them instead of carrying two byte-identical copies of the
  header-merge and cookie-jar update logic.
- `contrib/staticfiles.py` directory-index and file paths now share a
  `_is_under_root(real_path)` method that uses `os.path.commonpath`,
  replacing the `startswith(root + sep)` vs `commonpath` inconsistency.
- `middleware/logging.py` falls through to the parent logger hierarchy
  via `hasHandlers()` so adding `LoggingMiddleware` on top of a
  `logging.basicConfig`-configured app no longer attaches a duplicate
  handler. `setLevel(INFO)` is now unconditional so the start-time
  capture in `process_request` is always armed.
- `serving/protocol.py` logger name unified to
  `veloce.serving.protocol` (was a mix of `veloce.protocol` and
  `veloce.serving`).
- Six raw `inspect.iscoroutinefunction` call sites (`app.py`,
  `background.py`, `dependency.py`, `views.py`, two in
  `_handler_plan.py`) now route through the memoized
  `_internal._is_async_callable` wrapper.
- `routing/__init__.py` re-exports `Converter` and `register_converter`
  (already top-level-exported via `veloce/__init__.py`); the asymmetry
  is gone.
- `http/__init__.py` re-exports `Cookies`, `QueryParams`,
  `parse_multipart_form`, and `HeaderSet`. `HeaderSet` is the return
  type of `Response.vary` / `Response.allow`, so users receiving one
  from a property can now construct one through the documented public
  path.

### Removed

- Dead constants `K_DEFAULT` and `K_NONE` from `_handler_plan.py` —
  declared but never instantiated.

### Docs

- `Blueprint.register_blueprint` docstring clarifies that nested-route
  endpoint names are stored as `<child.name>.<handler>` on the parent
  and only pick up the `<parent.name>.` prefix at app registration
  (the eventual `<parent>.<child>.<handler>` shape was already
  produced; the previous wording implied it was applied immediately).

## [0.1.3] - 2026-05-23

Security + correctness batch. Fifteen issues from the second external
review of the published `veloceframework==0.1.2` wheel — eleven
correctness bugs, three security fixes, and a polish bundle.

### Security

- **CSRF token now rotates on demand** (#90). `CSRFMiddleware` no longer
  reuses an anonymous session's CSRF cookie after the user authenticates
  — that was a session-fixation pathway. Call the new
  `from veloce.middleware import rotate_csrf_token; rotate_csrf_token(request)`
  helper at the end of any login / logout / permission-elevation
  handler; `process_response` then mints a fresh signed token cookie.
- **`verify_password` refuses tampered scrypt cost parameters** (#85).
  A tampered stored hash carrying `N=2` would previously verify in
  microseconds, defeating scrypt's work floor. The verifier now
  rejects any stored hash whose `N` is below `2**14`, `r` is below 1,
  or `p` is below 1. The floor is hardcoded against a documented
  v0.1.3 minimum (not the live `_SCRYPT_N` default) so a future
  tune-up of the default does not retroactively invalidate hashes
  generated by earlier Veloce releases.
- **Swagger UI and ReDoc loaded with Subresource Integrity hashes** (#83).
  `/docs` and `/redoc` now pin specific patch versions
  (`swagger-ui@5.18.2`, `redoc@2.1.5`) and load with `integrity=` +
  `crossorigin="anonymous"`. A CDN compromise can no longer inject
  arbitrary JavaScript onto either page. Swagger UI moved from
  `unpkg.com` to `cdnjs.cloudflare.com` because cdnjs publishes the
  SRI hashes alongside their library mirror.

### Fixed

- **`FileResponse(path)` warns on the event loop** (#79). The sync
  constructor previously did an `open(path).read()` on the running
  loop, stalling every concurrent request for the duration of the
  read. The constructor now emits a `DeprecationWarning` when called
  with a loop running and points callers at `await
  FileResponse.from_path(path)` (or `asyncio.to_thread(...)` from sync
  code). The next major bump will tighten this to a hard error; for
  now the established sync helpers (`send_file`, `send_static_file`)
  keep working unchanged.
- **`Veloce()`'s first-request lock binds to the running loop**, not
  the loop current at construction (#80). The lock is allocated lazily
  inside `_run_request` the first time it is needed, so `Veloce()`
  instantiated at module scope and driven by `TestClient` (or any
  other later-spawned loop) no longer raises
  `RuntimeError: ... attached to a different loop`.
- **`TestClient.receive()` returns `http.disconnect` past end-of-body**
  (#81). The previous implementation awaited a fresh
  `asyncio.Event().wait()` that was never set, hanging legitimate ASGI
  middleware that read past `more_body=False`.
- **TestClient multipart escapes name + filename** (#82). The encoder
  now rejects `\r\n` in field/file names and backslash-escapes embedded
  `"` so a malformed name cannot break the MIME boundary or inject
  header fields.
- **Response body assignment invalidates the encode cache** (#89). A
  middleware that wrote `response.body = ...` after a prior `.encode()`
  used to emit stale bytes plus the wrong `Content-Length`. The `body`
  slot is now a property whose setter clears the cached `_encoded`
  blob.
- **Error handlers can receive the real failing request** (#86).
  `Veloce.handle_http_exception(exc, request=...)` and
  `handle_user_exception(exc, request=...)` now accept an optional
  request parameter; in-request callers pass the real one, out-of-band
  callers (background tasks, CLI hooks) still get a synthetic `GET /`.
- **`Router._merge_node` preserves route constraints** (#88).
  `include_router` no longer silently drops `subdomain`, `host`,
  `defaults`, `callbacks`, `openapi_extra`, `trailing_slash`, or
  `tolerant_slash` when merging a sub-router. Each was reconstructed
  without those fields previously.
- **`RateLimitMiddleware` periodic sweep is lock-guarded** (#84). Two
  concurrent requests entering the sweep window cannot both rebuild
  the bucket dict. Single-threaded asyncio already serialised this
  block, but a future async cache backend that introduced an `await`
  inside the sweep would have opened a race; the lock is defensive.
- **Four runtime guards stop relying on `assert`** (#87 + follow-up).
  `router.py:487` (radix-tree converter pin), `app.py:950` (middleware
  decorator type check), `serving/protocol.py:91` (asyncio
  full-duplex transport check), and `testclient.py:738`
  (WebSocket-accept message-type check) were all stripped under
  `python -O`. Replaced with explicit `raise` so the guard survives
  optimised deployments.

### Changed

- **`_pydantic_to_schema` logs schema-generation failures at WARNING**
  (#91) instead of silently degrading the affected model to
  `{type: object}`. The fallback still applies so `/docs` keeps
  rendering, but developers now have a log trail to diagnose the
  underlying issue. Set `logging.getLogger("veloce.openapi").setLevel(DEBUG)`
  to capture the full traceback.
- **`RateLimitMiddleware` docstring documents the in-process scope**
  (#93). The class is single-process by design; multi-worker
  deployments will see an effective limit of roughly `N × max_requests`
  per window. The docstring now points readers at nginx `limit_req` or
  a Redis-backed implementation for cross-worker accuracy.

### Polish

Issue #92 — six low-severity items from the same review batch:

- **L1**: `urlencode` hoisted to module top of `routing/router.py` so
  the `url_for` hot helper does not re-import on every external link.
- **L2**: redundant inline `import orjson` removed from
  `Response.get_json` (already imported at module top).
- **L4**: `TestClient` ASGI scope `raw_path` now UTF-8 encoded instead
  of ASCII, so tests that hit a non-ASCII URL no longer crash with
  `UnicodeEncodeError`. Applies to both the HTTP and the WebSocket
  scope builder.
- **L5**: `Session.update({})` no longer marks the session modified.
  Defensive empty-mapping calls stop forcing a `Set-Cookie` on every
  request. Iterator and generator arguments still flip `modified` —
  they can't be inspected for emptiness without consuming them, and
  the consumption already happened in `super().update`.
- **L6**: missing API key returns `401 Unauthorized` instead of `403
  Forbidden` (RFC 7235 §3 — 401 means "authentication required", 403
  means "authenticated but not authorised"). Applies to
  `APIKeyHeader`, `APIKeyQuery`, and `APIKeyCookie`.

## [0.1.2] - 2026-05-23

First post-release iteration. Closes four rough edges surfaced by the
end-to-end smoke test of the published `veloceframework==0.1.1` wheel.

### Changed

- **`Request.json()` is now `async`** (#74). Previous releases shipped
  `json()` as a synchronous method while `form()` was already `async`;
  the asymmetry broke the `await request.json()` idiom Starlette,
  FastAPI, and Quart callers reach for first. Migration: any call site
  that wrote `request.json()` now writes `await request.json()`. The
  Flask-flavoured `request.get_json()` stays synchronous so Flask
  muscle-memory continues to work.
- `pyproject.toml` runtime dependencies extended with `uvicorn[standard]`,
  `jinja2`, and `click` (#77). They were declared only in the dev
  dependency group on 0.1.1, so a fresh `pip install veloceframework`
  left users without `uvicorn` on the path and without `jinja2` for the
  templating helpers the docs point at. WebSocket support stays opt-in
  through the new `veloceframework[ws]` extra (`pip install
  veloceframework[ws]`) so REST-only deploys do not pull the
  `websockets` library.
- `veloce.__version__` is now derived from package metadata via
  `importlib.metadata.version("veloceframework")` (#75), with a literal
  fallback for editable installs without materialised metadata. The
  hand-maintained constant in `__init__.py` could and did drift from
  the wheel's `pyproject.toml` version (`0.1.0` vs `0.1.1` on the
  previous release); deriving from metadata makes the two impossible
  to disagree.

### Added

- `render_template`, `render_template_string`, and `Jinja2Templates`
  are now exported from the top-level `veloce` package (#76). The
  helpers always lived under `veloce.contrib.templating`; surfacing
  them at the root matches the Flask muscle-memory that the rest of
  the Veloce API preserves (`g`, `flash`, `current_app`,
  `before_request`, `redirect`, `make_response`, `abort`, `url_for`).

## [0.1.1] - 2026-05-23

Metadata-only release. No code, behaviour, or dependency changes
against 0.1.0 — this version exists solely to correct the maintainer
email recorded in the PyPI package metadata.

### Changed

- `pyproject.toml`: `authors` and `maintainers` email corrected from
  `revanthravella@gmail.com` to `lokeshtallapaneni@gmail.com`. The PyPI
  v0.1.0 metadata is immutable, so the fix lands as 0.1.1; users on
  v0.1.0 will pick up the corrected metadata on the next
  `pip install --upgrade veloceframework`.

## [0.1.0] - 2026-05-23

First public release. Veloce is published to PyPI as `veloceframework`;
the import name `veloce` is unchanged.

### Highlights

- Async-first ASGI core with a hand-written radix-tree router, custom
  request/response pipeline, in-memory `TestClient`, and a dependency
  injection system that resolves precompiled plans (`HandlerPlan`) at
  registration time so the per-request hot path performs no reflection.
- Feature surface covers Flask 3.x and FastAPI parity for the workflows
  most apps reach for first — blueprints, dependency injection, OpenAPI
  generation, Jinja templating, WebSockets, sessions, signals, and a
  complete Werkzeug-shape request/response API.
- Performance contract: comparative benches in `benchmark.py` show
  3-5x throughput vs equivalent FastAPI handlers and 4-7x vs Flask on
  the JSON-hello and path-param hot paths.

### Added

The entries below were authored during the `[Unreleased]` window and
ship as part of this release.

### Changed

- **Per-request dispatch +21-39 % (profile-driven DSA pass).** Walked the
  json-hello / path-param hot path under `cProfile` and applied seven
  targeted shaves, each attributed to a measured delta:
  * `Veloce._dispatch_request` defers `DependencyResolver()` until a
    non-trivial route demands it. Trivial-plan routes (no injected
    params, no dependencies) never construct the resolver — saves the
    resolver allocation + two attribute writes per static-GET request.
  * `DependencyResolver.__init__` no longer allocates a throwaway
    `dict` + `WeakKeyDictionary` for `_overrides` / `_override_subplans`;
    they default to module-level empty sentinels and the dispatcher
    swaps in the real instances only when overrides exist.
  * `Request.headers` is now a lazy property backed by `_headers_raw`
    (raw ASGI `(bytes, bytes)` tuples). The `CIMultiDict` + per-tuple
    `latin-1` decode is built only on first read. The hot path never
    reads `request.headers`, so 2-3 us / req of work was being burned
    on every dispatch.
  * `_run_response_middleware` is gated at the main hot-path return
    so the no-op coroutine + await is skipped when no middleware is
    registered (avoids ~940 ns / req of frame setup).
  * `_asgi_app` reads `scope["path"]` / `scope["query_string"]` via
    subscript (ASGI mandates both keys), skipping `dict.get` default
    handling.
  * Built-in `Content-Type` strings (`application/json`,
    `text/html; charset=utf-8`, `text/plain; charset=utf-8`,
    `application/octet-stream`) and small `Content-Length` values
    (0-2047) hit precomputed bytes caches; the per-request
    `_reject_header_crlf(...).encode()` + `str(n).encode()`
    allocations are skipped on cache hit.
  * `Response._stream` joins `__slots__` initialised to `None`; the
    `is_streamed` / `freeze` / `iter_encoded` / `iter_chunked` /
    `cache_control` lookups become a direct slot load instead of a
    `getattr(..., None)` walk.
  In-loop bench (`bench/hot_dispatch_bench.py`) median of 3 runs:
  static GET 68.1k → 94.8k req/s (+39 %),
  path-param GET 54.5k → 66.0k (+21 %),
  POST 64-byte body 62.0k → 77.2k (+25 %).
  cProfile total time for 16k mixed dispatches dropped 1.51 s → 0.59 s
  (~2.56×).
- **Stdlib `json` dropped in favour of `orjson` at the remaining two
  sites.** `Config.from_prefixed_env`'s default `loads` is now
  `orjson.loads`; `Config.from_file`'s default `load` is a new tiny
  `_orjson_load(fp)` adaptor (orjson has no file-object loader).
  `Swagger UI` HTML render emits `swagger_ui_parameters` and
  `swagger_ui_init_oauth` via `orjson.dumps(...).decode()`. orjson
  produces compact JSON (no space after `:`); the on-wire format
  for embedded literals is now `"key":value` rather than `"key": value`
  — the whitespace was never part of any contract and the JS parser
  consumes both identically. Behaviour-equivalent for valid JSON;
  catch sites unchanged because `orjson.JSONDecodeError` is a
  `ValueError` subclass.
- **Per-request dispatch ~+17 %.** Profile-driven pass over the in-loop
  ASGI hot path: `_setup_openapi` gated at call sites so the no-op
  branch costs one attribute read instead of a frame; `_endpoint_blueprint`
  no longer parsed three times per request when no blueprint hooks are
  registered; `Headers`, the `current_app`/`current_request` contextvars,
  and the `request_started`/`request_finished` signals hoisted to module
  top instead of being re-imported per request; single-chunk request
  body fast-path skips the `body_parts` list + `b"".join`;
  `_reject_header_crlf` inlined as three short-circuited `in` checks;
  `Signal.has_receivers_for` short-circuits on empty subscriber list;
  `_run_teardowns` await skipped when no yield-dependencies registered.
  In-loop bench (`bench/hot_dispatch_bench.py`): static GET 62.5k →
  72.8k req/s (+16.5 %), path-param 47.8k → 57.0k (+19.4 %), POST 64-byte
  body 54.8k → 64.2k (+17.2 %).
- **Router micro-ops.** `Router.match` tries the raw method on
  `handlers.get` before `method.upper()` (RFC-conforming clients send
  uppercase already); `_match_node` flattens static-only descent into a
  `while` loop when the current node has no param/wildcard alternatives,
  shaving one Python frame per static segment; single-param-child path
  skips the rollback `del` (no alternative to back off to);
  `FloatConverter.match` checks `"e" / "E"` directly instead of
  allocating `value.lower()`.
- **Per-request rate-limit O(N) → O(1).** `RateLimitMiddleware` switches
  from per-request list comprehension to `collections.deque` +
  amortised `popleft`. Periodic eviction sweep now mutates in place
  with a snapshot-then-recheck guard so an append racing with the
  sweep is not silently dropped.
- **Override-dependency sub-plan cache hoisted to the app.** Each
  request's fresh `DependencyResolver` shares
  `Veloce._override_subplans`, eliminating the per-request `build_plan`
  + triple `inspect.is*function` probe on override hits. The cache is
  cleared when `dependency_overrides` is reassigned.
- **Mount-prefix slash precomputed.** `_mounted_apps` /
  `_asgi_mounts` now store `(prefix, prefix + "/", app)` so dispatch
  doesn't reallocate `prefix + "/"` per request per mount.
- **Exception-handler signature cache.** `_call_exc_handler` memoises
  `(wants_request, wants_exc)` flags per handler in a
  `WeakKeyDictionary`, eliminating the `inspect.signature` walk per
  raised exception.
- **`jsonable_encoder` primitives short-circuit.** The
  `None | str | int | float | bool` branch is hoisted to the top of
  the dispatch so leaf calls hit it before any of the heavier
  `isinstance` checks.

### Fixed

- **ETag drift between StaticFiles and FileResponse.** `StaticFiles._compute_etag`
  now delegates to `veloce.http.response._file_etag` so a static handler
  and `FileResponse` over the same file emit identical ETags and
  validate identically against `If-None-Match`. Signature changed
  from `(path, mtime)` to `(path, size, mtime)` — `_`-prefixed and
  therefore private, but flagged for subclassers.
- **WebSocket ASGI-mode unbounded queue.** `WebSocket.from_asgi`
  builds `_receive_queue` with `maxsize=DEFAULT_RECV_QUEUE_MAXSIZE`
  instead of an unbounded queue. The queue is unused in ASGI mode
  today; the bound prevents a footgun if future changes start feeding
  it.

### Added

- **Comparative bench harness** (`bench/comparative/`): head-to-head
  latency and throughput measurements vs Flask and FastAPI under the
  same uvicorn runtime. Each workload runs all three frameworks in
  randomised order through a single `httpx.AsyncClient`, with a
  discarded cold-cache round to dampen first-run penalties. Reports
  median rps, p50, p99. `--seed` pins the schedule for reproducibility.
  Initial workloads: `json-hello` and `path-param`. Results recorded
  under `docs/bench/`. Veloce wins rps + p50 + p99 vs FastAPI on both
  workloads, and wins rps vs Flask by ~57 % (Flask wins p50/p99 under
  `asgiref.WsgiToAsgi` at low concurrency — see caveats in
  `docs/bench/README.md`).
- `WebSocket.origin` accessor returns the handshake `Origin` header
  (or `None`); `WebSocket.check_origin(allowed)` returns `True` only
  when the origin is on the allow-list. Normalisation
  (`.rstrip("/").lower()`) and wildcard (`"*"`) semantics match the
  registered-once `WebSocketOriginMiddleware`, so allow-lists are
  interchangeable between the two APIs. `Origin: null` (sandboxed
  iframes / `file://`) is rejected. The pair lets handlers reject
  Cross-Site WebSocket Hijacking before `accept()` — the WebSocket
  handshake is plain HTTP, so Same-Origin Policy and CORS do not apply.
- **Application core** — `Veloce` app object with HTTP method decorators
  (`get`/`post`/`put`/`patch`/`delete`/`head`/`options`/`trace`),
  lifespan handling, configurable docs URLs, and `app.run()`.
- **Radix-tree router** — typed path converters (`int`, `float`, `str`,
  `uuid`, `path`, custom registered converters), per-route
  `strict_slashes`, subdomain and host constraints, and rule defaults.
- **Request / Response** — lazily-parsed `Request` with multi-value
  query/header/cookie/form accessors, parsed conditional and range
  headers, and a `Response` family (`JSONResponse`, `HTMLResponse`,
  `PlainTextResponse`, `RedirectResponse`, `StreamingResponse`,
  `FileResponse`, `ORJSONResponse`, `UJSONResponse`).
- **Dependency injection** — `Depends` / `Security` / `SecurityScopes`,
  `yield`-style dependencies with teardown, `Annotated[...]` form,
  bare `Depends()` annotation inference, and `app.dependency_overrides`.
- **Parameters** — `Query`, `Path`, `Header`, `Cookie`, `Body`, `Form`,
  `File` markers with constraints (`ge`/`le`/`gt`/`lt`, `min_length`/
  `max_length`, `pattern`, `multiple_of`), list-valued collection, and
  `include_in_schema` / `title` / `examples`.
- **OpenAPI 3.1** — auto-generated schema, Swagger UI and ReDoc, security
  scheme emission, route-level `responses` / `callbacks` /
  `openapi_extra`, and a documentation-only `app.webhooks` router.
- **WebSockets** — ASGI WebSocket routing, raw message API, JSON/text/
  bytes iteration, subprotocol negotiation, dependency injection, and
  `WebSocketException` / `WebSocketRequestValidationError`.
- **Middleware** — CORS, GZip, TrustedHost, HTTPS redirect, sessions,
  rate limiting, request ID, proxy header handling, plus a
  dispatch-style base class.
- **Templating** — Jinja2 integration with async rendering, context
  processors, and registered filters/globals/tests.
- **Sessions** — signed, timestamped cookie sessions with secret
  rotation, a mutation-tracking `Session` container, and persistent
  (`permanent`) sessions.
- **Utilities** — background tasks, signals, Server-Sent Events,
  blueprints with nesting, class-based views, a CLI (`veloce run` /
  `veloce routes` / `veloce shell`), and an in-memory `TestClient`.
- **Security helpers** — HTTP Basic / Bearer / Digest, API key schemes,
  OAuth2 password and authorization-code flows, password hashing, and
  signed-value serialisation.
- `veloce.status` gains `HTTP_208_ALREADY_REPORTED`, `HTTP_226_IM_USED`,
  and `HTTP_421_MISDIRECTED_REQUEST` for full IANA HTTP status coverage.
- `constant_time_compare(a, b)` — a timing-safe secret-comparison helper
  (wrapping `hmac.compare_digest`), exported from the top-level package.
- Veloce's `WebSocket` frame parser reassembles fragmented messages —
  a `FIN=0` data frame followed by continuation frames (RFC 6455 §5.4);
  control frames may be interleaved without disturbing the in-progress
  message.
- `add_middleware` now accepts a standard ASGI middleware class — any
  class that is not a veloce `Middleware` subclass is treated as ASGI
  middleware and wraps the whole application (`cls(app, **options)`),
  so the third-party ASGI ecosystem (tracing, profiling, observability)
  plugs into a veloce app. The first-registered ASGI middleware is the
  outermost wrapper. Native `Middleware` classes are unaffected.
- `AsyncTestClient` (and the `app.async_test_client()` factory) — the
  async counterpart of `TestClient`. Used as `async with` inside an
  async test, its request methods are coroutines awaited on the test's
  own running event loop. Cookie persistence, redirect following, and
  the JSON / form / files body shapes match `TestClient`.
- `app.mount(prefix, app)` now accepts any ASGI application, not only a
  veloce sub-app. A non-veloce app is dispatched at the ASGI layer with
  the matched prefix moved from the scope's `path` onto `root_path`;
  veloce sub-apps keep their existing dispatch path. A mounted ASGI app
  receives `http` and `websocket` scopes — the parent app owns the
  `lifespan` cycle, so a mounted app must self-initialise rather than
  rely on ASGI `lifespan` events. Mount prefixes must not overlap.
- `Config.from_env_file(path)` loads a dotenv-style `.env` file —
  `KEY=VALUE` lines, `#` comments, an optional `export ` prefix, and
  quoted values — into the app config (UPPERCASE keys only).
- `app.run(ssl_context=...)` — the built-in development server now
  accepts an optional `ssl.SSLContext`, handed straight to
  `loop.create_server(ssl=...)`, for local HTTPS testing. Left unset the
  serving path is byte-for-byte the same plain-HTTP path as before.
  Production should still terminate TLS at uvicorn or a reverse proxy.
- `EventLoopWatchdog` — an opt-in development aid that detects a
  coroutine blocking the event loop (a synchronous driver, `time.sleep`,
  a CPU-heavy loop) and logs a warning carrying the blocked stack and a
  prescriptive hint (blocking-I/O vs CPU-bound). A loop heartbeat plus a
  separate daemon thread spot the stall. Enable it with the
  `EVENT_LOOP_WATCHDOG` config key; unset (the default) nothing is
  constructed, so a production app pays nothing.
- `ServerSessionMiddleware` keeps the session payload server-side in a
  pluggable `SessionStore` (default: an in-process `InMemorySessionStore`)
  — the cookie carries only an opaque, high-entropy session id. Sessions
  are now *revocable*: empty one in a handler (`session.clear()`) or
  delete it straight from the store (`await store.delete(session_id)`),
  and a tampered or stale cookie simply fails to resolve. A network
  backend (e.g. Redis) plugs in by implementing the async `SessionStore`
  interface. The existing signed-cookie `SessionMiddleware` is unchanged.
- `app.add_instrumentation(hook)` registers an observability hook called
  once per finished HTTP request with a `RequestMetrics` record — method,
  concrete path, matched route *template* (a low-cardinality metric
  label), status code, and wall-clock duration. Hooks may be sync or
  async; one that raises is logged and never breaks the response. With no
  hook registered the request path pays nothing — not even a clock read.
  The `request_started` / `request_finished` signals now also carry the
  `Request`, so a tracing bridge can correlate a request's start with its
  finish.
- Query / path / header / cookie parameters are now validated through
  Pydantic for any annotation the fast scalar path does not cover —
  `datetime`, `date`, `time`, `UUID`, `Decimal`, `Literal[...]` and other
  rich types are parsed and rejected with a `422` on bad input, the same
  treatment a request-body model already received. The `str` / `int` /
  `float` / `bool` / `Enum` dispatch fast path is untouched. OpenAPI
  parameter schemas now emit the matching `format` / `enum` keywords
  instead of collapsing every non-primitive to a bare string.

### Changed

- A handler that returns a bare `str` now defaults to
  `Content-Type: text/html; charset=utf-8` (previously `text/plain`), so a
  bare-`str` return and `make_response(str)` produce the same media type.
- Multipart form parsing now uses the `python-multipart` streaming
  parser instead of an in-memory `body.split` — it correctly handles a
  boundary token that happens to occur inside binary file data, and a
  malformed body degrades to the parts that parsed cleanly rather than a
  `500`.
- `app.run()` starts the built-in **development** server; it now logs a
  startup reminder that production deployments should run under uvicorn
  (or another ASGI server). See the new Deployment guide.
- `Response.set_cookie` now defaults `samesite` to `"Lax"` — a
  CSRF-resistant default that matches modern browser behaviour. Pass
  `samesite=None` to omit the attribute, or `"None"` (with `secure=True`)
  for a genuinely cross-site cookie.
- WebSocket dependency injection now runs through the same pre-planned
  `HandlerPlan` / `DependencyResolver` as HTTP dispatch. WebSocket
  dependencies gain `yield`-style teardown and `Security` /
  `SecurityScopes` support, and path parameters are coerced to their
  annotated type — previously WebSocket DI used a separate, weaker
  resolver that supported none of these.
- An uploaded file is now backed by a `SpooledTemporaryFile`: each
  multipart part streams into one as it is parsed, staying in memory
  while small and rolling over to a real temp file on disk once it grows
  past 1 MiB. A large upload no longer holds two or three full copies of
  itself in RAM (raw body + per-part `bytearray` + `BytesIO`).
- `request.stream()` now yields the body in bounded 64 KiB chunks instead
  of one chunk covering the whole body, so a handler can process a large
  body incrementally.

### Fixed

- `register_blueprint` no longer drops a blueprint's routes registered with
  `include_in_schema=False`, nor its WebSocket routes — every route is added
  to the radix tree.
- `EventSourceResponse` encodes yielded `ServerSentEvent` objects over the
  ASGI transport instead of raising `TypeError`.
- Dependency type hints are resolved from the right object for class
  dependencies (`__init__`), callable instances (`__call__`), and
  `functools.partial` wrappers, so their parameter types are coerced.
- The in-memory `TestClient` accepts WebSocket connect paths that include a
  query string.
- `WebSocket.send()` — the raw ASGI-message escape hatch — now enforces the
  same handshake state machine as `send_text` / `send_bytes`: sending before
  `accept()` or after `close()` raises instead of proceeding silently.
- `WebSocket.receive_text` / `receive_bytes` / `receive_json` enforce the
  same handshake state machine as their `send_*` siblings: calling them
  before `accept()` raises `RuntimeError` (was: hung on an empty queue)
  and calling them after `close()` raises `WebSocketDisconnect`.
- Multipart parsing no longer leaks a `SpooledTemporaryFile` when a request
  is rejected by a DoS cap (oversized part or too many parts): the
  in-progress part's spool and every spool already collected from
  completed parts are closed on the reject path.
- Server-side sessions use a conditional store write: a session revoked by
  a concurrent request (logout, `store.delete(...)`) while another request
  is in flight is no longer resurrected when that request writes back —
  `SessionStore` gains a race-safe `replace()` method.
- HTTP requests each get their own `DependencyResolver` instead of sharing
  one: a concurrent request can no longer clear another in-flight request's
  pending `yield`-dependency teardowns (database session close, file-handle
  release), which previously could be silently skipped under load.
- `Config.from_env_file` strips an unquoted inline ` #` comment from a
  `.env` value (`KEY=value  # note` → `value`) while leaving a `#` inside a
  quoted value intact.
- `app.mount()` rejects an overlapping prefix registration (a prefix equal
  to, nested under, or containing an existing mount) with `ValueError`,
  instead of silently shadowing one mount with another.
- `Request.files()` no longer returns duplicate `UploadFile` entries, nor
  runs in O(n²), when several files are uploaded under one form field
  name — it now iterates the form's `(key, value)` pairs once.
- `StaticFiles._etag_cache` is now a bounded LRU (default cap 1 024 entries
  per instance, configurable via the class attribute `ETAG_CACHE_MAX`).
  The previous unbounded dict grew for the lifetime of the worker on a
  large static tree.
- `LoggingMiddleware` no longer keeps a per-instance dict keyed by
  `id(request)`. The start timestamp lives on `request._state`, so it
  cannot leak when a handler raises, and a recycled `id()` cannot
  collide with a stale entry to log a nonsensical duration.
- `jsonable_encoder(obj, include=..., exclude=...)` forwards the filters
  into recursive calls — `exclude={"password"}` now strips the field at
  every depth, matching the dataclass branch.
- `Signal` actually filters by sender. A receiver connected with
  `signal.connect(fn, sender=X)` fires only when `send(X)` runs (matched
  by `is`, falling back to `==`). A receiver connected with the default
  `sender=ANY_SENDER` (sentinel re-exported from `veloce.signals`) still
  fires for every send.
- `UploadFile.read`/`write`/`seek`/`close` now offload the blocking
  filesystem syscalls to a thread once the spool has rolled over to
  disk; the cheap in-memory `BytesIO` path stays on the loop.
- `hash_password_async` / `verify_password_async` — async-safe wrappers
  that run the scrypt KDF on a thread. The sync `hash_password` /
  `verify_password` are unchanged; calling either from an `async def`
  handler blocks the loop for ~100 ms, so async handlers should reach
  for the `_async` variants. Both are exported from the top-level
  package.
- `WebSocket._receive_queue` is now bounded (default `maxsize=64`;
  configurable via the `recv_queue_maxsize` constructor argument). The
  cap turns the previously-unbounded queue into a backpressure signal:
  a peer that sends faster than the handler reads now blocks the
  producer on `put` instead of growing the queue without limit.
- All four wall-clock perf checks in the test suite —
  `TestPerformanceAfterFixes` (`tests/test_async_safety.py`),
  `TestNoSyncIOInHotPath` (`tests/test_async_io.py`), and
  `TestPerformance` (`tests/test_iteration3.py`) — are now marked
  `@pytest.mark.perf` and excluded from the default `pytest` run via
  `addopts = ["-m", "not perf"]` in `pyproject.toml`. The
  relative-to-async budget alone was still flaky under full-suite CPU
  contention; opt in with `pytest -m perf` on a quiet machine. Catastrophic
  dispatch regressions remain gated in CI by
  `bench/dispatch_bench.py --min-rps 2000`.
- Documentation corrected against the code: sync (`def`) handlers are
  documented as supported (run in a thread-pool executor); the built-in
  development server is documented as HTTP/1.1-only (WebSocket and HTTP/2
  workloads run under an external ASGI server); the shipped
  `ServerSessionMiddleware` / `SessionStore` replaces a stale "on the
  roadmap" note; and scoped request hooks are clarified as a `Blueprint`
  feature, not a plain `Router` one.

### Performance

- Radix-tree param-child lookup is now O(1) at registration via a
  `(param_name, converter_type)` sidecar index (CL40 / CL41). The
  ordered list remains the source of truth at match time so traversal
  semantics are unchanged. `_split_path` also drops its `strip("/")`
  pass — the empty-string filter on the split already handles
  leading, trailing, and consecutive slashes.
- Independent sibling `Depends()` slots now resolve in parallel via
  `asyncio.gather` when safe (CL10). The resolver looks ahead for
  contiguous `K_DEPENDS` siblings and dispatches them concurrently
  unless the run contains a `Security()` scope-pushing slot, a
  yield-style dependency, or two siblings sharing a `use_cache=True`
  callable — those cases preserve the sequential semantics that
  protect the resolver's shared cache, security-scope stack, and
  teardown ordering. Handlers with multiple I/O-bound deps now wait
  for `max(durations)` instead of `sum(durations)`.
- Blueprint hooks (`before_request`, `after_request`,
  `teardown_request`) are now bucketed by blueprint at registration
  (CL22). Dispatch reads the bucket for the matched route's endpoint
  prefix instead of iterating every blueprint's gated wrapper and
  doing a `startswith` no-op on each — eliminates the O(B·H)
  per-request overhead for apps with many blueprints.
- `StaticFiles` streams files at or above `STREAM_THRESHOLD` (1 MiB
  default) via `StreamingResponse` instead of buffering the whole
  body (CL4). Worker RSS no longer grows by the file size for the
  duration of a large download. Range requests still buffer the
  slice, which is already bounded by the client.
- `Request.mimetype` and `Request.mimetype_params` now share a single
  cached parse (CL14): the first access populates `_parsed_ct` with a
  `(mimetype, params)` tuple; subsequent reads on either property hit
  the cache. Handlers that touch `content_type` repeatedly (form
  parsers, validators) stop re-splitting the same string per access.
- `_find_exception_handler` now memoises the MRO walk per exception
  type. The cache is cleared whenever a new error handler is
  registered so a fresh registration takes effect for previously
  cached subclasses.
- `SignedSerializer.loads` does a single `token.split(".", 2)`
  instead of `token.count(".") != 2` + `token.split(".")`. The
  early-validation path is now one pass over the string.
- `LoggingMiddleware` short-circuits when the logger has the access
  level disabled — both the `time.monotonic()` clock read on entry
  and the duration calculation on response.
- `SessionMiddleware._cookie` / cookie composition use
  `"; ".join(parts)` instead of a chain of `+=` concatenations,
  cutting intermediate string allocations.
- `StaticFiles` directory listing reads `is_dir` from `os.scandir`'s
  cached dirent — saves a per-entry `os.path.isdir` syscall.
  Behaviour note: symlinks inside a listed directory are now classified
  via the symlink itself, not its target — a symlink to a subdirectory
  renders as a plain entry rather than a directory entry. This avoids
  advertising symlink targets in the listing and matches the
  symlink-safety stance the static handler already takes.
- `Response.encode()` no longer rebuilds the header dict on every
  response: the three framework defaults (`Content-Type`,
  `Content-Length`, `Connection`) are emitted inline only when the
  caller has not supplied them. A case-insensitive check at the same
  time removes a latent duplicate-header bug where a user-supplied
  `"content-type"` (lowercase) would land alongside the default
  `"Content-Type"` in the encoded line. Reason phrase comes from the
  module-level `{code: phrase}` map already added for `Response.status`.
- `StaticFiles` now satisfies the existence + stat with a single
  executor `os.stat` call, classifying file/dir from `st_mode` —
  the previous request path issued `isfile` and then a second `stat`
  for size/mtime, doubling executor round-trips.
- WebSocket `_send_frame` hands the header + payload to the transport
  as two separate buffers via `writelines` instead of
  `bytearray.extend(data)` + `bytes(frame)`. On a 64 KiB frame that
  saves a 64 KiB memcpy on the way out.
- SSE: single-line event payloads skip the `data.split("\n")` list
  allocation; chunked SSE writes use `transport.writelines` instead
  of concatenating size-line + body + trailer into one fresh bytes
  per chunk.
- SSE status-line reuses the response-module `_STATUS_PHRASES` table
  rather than `from http import HTTPStatus` + `HTTPStatus(code).phrase`
  on every stream startup.
- `http_date(None)` (the per-response `Date:` header) caches the
  RFC 9110 IMF-fixdate to one-second resolution — `formatdate()` ran
  once per response despite the output only changing once a second.
- App-level hook dispatch (`teardown_request`, `teardown_appcontext`,
  `_call_handler`) reads `iscoroutinefunction` from a memoised cache
  keyed by `id(fn)`. Hooks register once and are dispatched many times;
  the inline `inspect.iscoroutinefunction(...)` walk on every request
  is replaced by a dict lookup.
- `CORSMiddleware` precomputes `", ".join(self.allow_methods)`,
  `", ".join(self.allow_headers)`, and `", ".join(self.expose_headers)`
  at construction. Per-response preflight emission now hits the
  precomputed strings instead of rebuilding them on every cross-origin
  reply.
- Cookie-based `SessionMiddleware` drops the
  `json.dumps(sort_keys=True)` mutation tripwire (computed on entry
  *and* on exit) in favour of the `Session.modified` flag the
  `Session` container already maintains — saves two full
  serialisations on every request that traverses the middleware.
- WebSocket frame unmasking is now bulk-XOR via `int.from_bytes` /
  `to_bytes` over the tiled mask, replacing a Python-level per-byte
  loop. Saves measurable CPU on any frame past a handful of bytes
  (WebSocket payloads are usually hundreds to KiB-sized).
- `Response.status` reads the reason phrase from a module-level
  `{code: phrase}` map built once at import time, instead of
  constructing an `HTTPStatus(code)` IntEnum-walk on every access.
- `email.utils.parsedate_to_datetime` is now imported at module load
  in `veloce.http.request` instead of re-imported inside four
  conditional-request hot properties.
- `StaticFiles` caches `mimetypes.guess_type` per file path
  (bounded LRU, 512 entries) — `guess_type` was running its full MIME
  table walk on every static hit.
- `safe.secure_filename` uses a module-level compiled regex for the
  underscore-run collapser, removing the per-call `re` cache lookup.
- Per-request reflection eliminated from the hot path: handler
  signatures are inspected once at registration into a frozen
  resolution plan.
- Static route lookup is O(1) per tree level — static child nodes are
  indexed in a dict instead of scanned linearly, so match cost no longer
  grows with the number of sibling routes.
- Request dispatch matches each route once per request instead of twice.
- The dependency resolver no longer re-imports its slot-kind constants on
  every call, and `_call_handler` skips a per-request
  coroutine-function probe by reading the precomputed handler plan.
- `StaticFiles` resolves the served root's real path once at construction
  rather than on every request; the request-scoped `g` store is allocated
  lazily, so handlers that never touch `g` pay no allocation.
- A parameter's `pattern` / `regex` constraint is compiled once at
  declaration time instead of recompiled on every `validate` call.
- `CORSMiddleware` precomputes its origin allow-list as a frozenset and a
  lowercased header allow-set at construction, so per-request CORS checks
  are O(1) instead of scanning a list.
- `Jinja2Templates` `auto_reload` now follows the bound app's `debug`
  flag when left unset — production rendering skips the per-render
  template `stat` syscall. Pass an explicit `auto_reload=` to pin it.
- `response_model=list[Model]` dumps a handler-returned element that is
  already an instance of the target model directly, skipping a
  re-validation round-trip (and correctly preserving per-element
  `exclude_unset`, matching the scalar `response_model` path).
- The ASGI entry point decodes request headers via a list comprehension
  rather than a generator, trimming a per-header generator-frame resume.
- A route whose handler takes no injected parameters and declares no
  dependencies is now dispatched through a trivial-route fast path that
  skips the dependency resolver entirely instead of resolving to `{}`.

### Security

- `MAX_CONTENT_LENGTH` is now enforced incrementally — an oversized
  request body is refused with `413` while still being received, before
  the whole payload is buffered into memory.
- The built-in development server enforces a request-read timeout
  (`HttpProtocol.REQUEST_TIMEOUT`, 30 s) — a half-sent request is dropped
  with `408`, bounding how long a slowloris-style slow client can pin a
  connection open.
- `WebSocketOriginMiddleware` rejects cross-site WebSocket handshakes
  (CSWSH) by checking the handshake `Origin` against an allow-list.
- `SecurityHeadersMiddleware` attaches `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, and optional HSTS / CSP /
  `Permissions-Policy` response headers.
- `CSRFMiddleware` accepts a `secret` that HMAC-signs the token (with an
  optional `max_age` expiry), so a cookie value carrying no valid server
  signature is refused — raising the bar against cookie-injection CSRF.
  The CSRF cookie now defaults to `Secure`.
- Multipart form parsing caps the part count and per-part size
  (`MAX_FORM_PARTS` / `MAX_FORM_PART_SIZE` config keys), raising `413`
  — a guard against algorithmic-complexity DoS from a maliciously
  structured form.
- `app.use_secure_defaults()` applies a hardened baseline (secure session
  cookies + `SecurityHeadersMiddleware`); `app.security_audit()` and the
  new `veloce check` CLI command report configuration risks before
  deployment.
