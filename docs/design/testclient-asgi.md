# Design: TestClient on the ASGI surface (T1)

## Contract

The synchronous test client drives the app via its **ASGI 3.0** interface —
`await app(scope, receive, send)` — rather than calling `app.handle_request`
directly. Doing so exercises the same code path a real ASGI server would
take: scope construction, header byte-list encoding, the receive/send
message loop, and (where relevant) lifespan handshake.

External API is unchanged from the previous TestClient:

- `client.get(path, headers=, params=)`
- `client.post(path, json=, data=, headers=, content=)` and the same for
  `put`/`patch`
- `client.delete/head/options(path, headers=)`
- `TestResponse.status_code`, `.body`, `.headers` (dict), `.content_type`,
  `.cookies`, `.json()`, `.text`, `.raw_headers` (the original
  `list[tuple[bytes, bytes]]` from ASGI).
- Cookies persist on the client across requests.
- `with TestClient(app) as c:` runs lifespan startup/shutdown.

## Why this matters

Earlier testing showed the old client was the root cause of several
bugs: it bypassed `app.__call__`, so
ASGI-shape bugs in middleware, response encoding, and cookie multiplicity
were invisible to the test suite. Routing the client through `app(scope,
…)` immediately surfaces them.

## Scope shape

```
{
  "type": "http",
  "asgi": {"version": "3.0", "spec_version": "2.3"},
  "http_version": "1.1",
  "method": "GET",
  "scheme": "http",
  "path": "/x",
  "raw_path": b"/x",
  "query_string": b"a=1",
  "root_path": "",
  "headers": [(b"host", b"testserver"), (b"x-custom", b"v"), …],
  "client": ("testclient", 50000),
  "server": ("testserver", 80),
}
```

Per ASGI 3.0 §HTTP. Header keys lower-cased, values latin-1 encoded.

## Receive / send

`receive()` yields one `http.request` message with the full body and
`more_body=False`. Subsequent awaits idle forever (per ASGI: well-behaved
apps don't read past end-of-body).

`send()` collects `http.response.start` (status + raw headers list) and
one or more `http.response.body` chunks, concatenating until `more_body`
is False or the call returns.

## Lifespan

Startup is run at construction (so user `state` mutations in startup hooks
land before the first call). Shutdown runs in `__exit__` / `close()`. The
new client deliberately does **not** synthesize lifespan ASGI scope
messages because `Veloce._run_lifecycle` already provides direct hooks;
running both would double-fire. When M1 introduces a real ASGI lifespan
manager this can switch to the protocol-level path.

## Cookies and multi-value Set-Cookie

`TestResponse` looks at all header tuples with key `set-cookie` and parses
each cookie's `name=value` head into `response.cookies`. Multiple cookies
on one response are all extracted. Cookie persistence across requests
copies them into the client's cookie jar by name.

The current `Response.set_cookie` joins multiple cookies into one header
value with `"\r\nSet-Cookie: "` — this is wrong on ASGI (each cookie
should be its own `(b"set-cookie", value)` tuple). The TestClient
tolerates the wrong shape for now; the underlying response bug lives in
the M1 task scope.

## Trade-offs

- **No `follow_redirects=True`** yet — deferred to Tier 1.
- **No `client.websocket_connect(...)`** yet — deferred to Tier 1 (needs
  W1/W8 first).
- **No httpx dependency** — we drive ASGI directly. httpx as a transport
  layer doesn't add value here because we already control both sides
  in-process. If users want httpx-shaped niceties they can use
  `httpx.AsyncClient(transport=httpx.ASGITransport(app))` themselves.
- Async-test usage (`@pytest.mark.asyncio`) of TestClient is unsupported
  because the client's `_loop.run_until_complete` would clash with the
  pytest-asyncio loop. This was also true of the previous client. Use
  the client from synchronous tests.

## References

- ASGI 3.0 spec §HTTP scope, §receive event, §send event.
- ASGI 3.0 spec §Lifespan protocol (deferred to M1).
