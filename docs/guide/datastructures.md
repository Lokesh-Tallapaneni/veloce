---
description: HTTP data structures on the request — the URL container, the case-insensitive multi-valued Headers, the parsed Accept header AcceptHeader, Range parsing with RangeSpec, and the parsed Authorization header.
tags: [request, headers, parsing, http]
---

# HTTP data structures

Veloce parses the raw request line and headers into typed objects you read off
the [`Request`](../reference.md#veloce.Request). [`URL`](../reference.md#veloce.URL),
[`Headers`](../reference.md#veloce.Headers),
[`AcceptHeader`](../reference.md#veloce.AcceptHeader),
[`RangeSpec`](../reference.md#veloce.RangeSpec), and
[`Authorization`](../reference.md#veloce.Authorization) are all importable from the
top-level package, but you rarely construct them yourself — each surfaces as a
cached property on the request. These types ship with Veloce; nothing extra to install.

!!! note
    These structures describe the **incoming request**. A `Response` carries plain
    `dict[str, str]` headers (set with `response.set_cookie()`, `response.headers[...]`,
    and the typed setters in [Responses (Advanced)](responses-advanced.md)); it does not
    expose a `Headers` object.

## Headers

`request.headers` is a [`Headers`](../reference.md#veloce.Headers) — a case-insensitive,
multi-valued mapping backed by `multidict.CIMultiDict`. Lookups ignore case, duplicate
header names are preserved, and `getlist` returns every value for a name (empty list when
absent, where `multidict`'s native `getall` would raise).

```python title="app.py"
from veloce import Headers, Veloce

app = Veloce()


@app.get("/inspect")
async def inspect(request):
    h = request.headers
    return {
        "ua": h.get("User-Agent", ""),       # case-insensitive
        "ua_lower": h.get("user-agent", ""),  # same value
        "encodings": h.getlist("Accept-Encoding"),
    }
```

Single-value access (`headers["x"]`) returns the first value. Construction from a plain
dict, a list of `(name, value)` tuples, or another multidict all work.

| Method | Returns | Purpose |
| --- | --- | --- |
| `headers["X"]` / `headers.get("X")` | First value | Case-insensitive single lookup. |
| `headers.getlist("X")` | `list[str]` | Every value for a name; empty list when absent. |
| `headers.to_wsgi_list()` | `list[tuple[str, str]]` | Insertion-ordered `(name, value)` pairs, duplicates kept. |
| `headers.copy()` | `Headers` | Shallow copy with the same entries. |

The `add` method appends a header and serialises optional `key=value` parameters,
double-quoting values that contain whitespace or punctuation and mapping `_` in
parameter names to `-`:

```python
from veloce import Headers

h = Headers()
h.add("Content-Disposition", "attachment", filename="my file.txt")
h.getlist("Content-Disposition")  # ['attachment; filename="my file.txt"']
```

## URL

`request.url` is a [`URL`](../reference.md#veloce.URL) — a parsed, lazily-constructed
view of the request target with component access. The scheme, host, and port are
resolved from the ASGI scope and the validated `Host` header.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/where")
async def where(request):
    url = request.url
    return {
        "scheme": url.scheme,
        "host": url.host,
        "port": url.port,
        "path": url.path,
        "query_string": url.query_string,
        "netloc": url.netloc,   # host[:port], port omitted when default
        "full": str(url),       # scheme://netloc/path?query#fragment
    }
```

`netloc` brackets IPv6 hosts and omits the port when it is the scheme default (80 for
`http`, 443 for `https`). `str(url)` rebuilds the full URL and is cached after the first
call. Use `url.replace(path="/other")` to derive a new `URL` with selected components
swapped — it returns a fresh instance and leaves the original untouched.

!!! warning "The `Host` header is validated, not trusted"
    A `Host` whose host component is not a valid RFC 3986 reg-name or IPv6 literal, or
    whose port is not a `1`–`65535` integer, degrades to `localhost` rather than flowing
    into `url.host` / `url.netloc`. This stops a Host-injection payload
    (`evil.com/path?x`, a CRLF-laced value) from poisoning absolute-URL construction.

Several request properties are thin views over `url`:

| Property | Value |
| --- | --- |
| `request.base_url` | `scheme://netloc` |
| `request.url_root` / `request.host_url` | `scheme://netloc/` |
| `request.scheme` | `url.scheme` |
| `request.is_secure` | `True` when the scheme is `https`. |

## AcceptHeader

The four `Accept-*` headers parse into an [`AcceptHeader`](../reference.md#veloce.AcceptHeader),
which models RFC 9110 §12.5 q-value semantics. Each is a cached property on the request:

| Property | Header | Matching |
| --- | --- | --- |
| `request.accept_mimetypes` | `Accept` | MIME wildcards (`text/*`, `*/*`) and media-type params. |
| `request.accept_languages` | `Accept-Language` | Plain string equality. |
| `request.accept_encodings` | `Accept-Encoding` | Plain string equality. |
| `request.accept_charsets` | `Accept-Charset` | Plain string equality. |

`best_match` does the negotiation: pass the options your handler can produce, in your
order of preference, and it returns the one the client accepts with the highest q-value.

```python title="app.py"
from veloce import JSONResponse, Response, Veloce

app = Veloce()


@app.get("/report")
async def report(request):
    best = request.accept_mimetypes.best_match(
        ["application/json", "text/html"]
    )
    if best == "text/html":
        return Response(body=b"<h1>Report</h1>", content_type="text/html")
    return JSONResponse({"report": "ok"})
```

When the client sends no `Accept` header (no preference expressed), `best_match` returns
the first option — a missing `Accept` means "accept anything". Among accepted candidates a
parameterized exact match beats a bare wildcard; ties go to your option order.

Other methods on the parsed header:

| Method | Returns | Purpose |
| --- | --- | --- |
| `quality(value)` | `float` | The q-value the client assigned; `0` when rejected or unmentioned. |
| `quality_explicit(value)` | `float` | Like `quality`, but an explicit token (even `q=0`) overrides a `*` wildcard. |
| `accepts_identity()` | `bool` | Whether the `identity` (no-encoding) coding is acceptable. |
| `values` | `list[str]` | Accepted values in the order the client sent them. |
| `value in accept` | `bool` | `True` when `quality(value) > 0`. |

```python
from veloce import AcceptHeader

acc = AcceptHeader.parse("br;q=1.0, gzip;q=0.8, identity;q=0")
acc.best_match(["gzip", "br"])   # 'br'
acc.quality("gzip")              # 0.8
acc.accepts_identity()           # False
```

!!! warning "`AcceptHeader` reports preference, it does not enforce it"
    `best_match` and `quality` tell you what the client prefers. Your handler still has to
    act on the result — choose the response media type, return `406 Not Acceptable`, or
    pick a content-coding. Reading the header alone changes nothing on the wire.

## RangeSpec

`request.range` parses the `Range:` header (RFC 9110 §14.2) into a
[`RangeSpec`](../reference.md#veloce.RangeSpec), or `None` when the header is absent or
unparseable. `unit` is the range unit (usually `"bytes"`) and `ranges` is a list of
`(start, end)` tuples, with `None` standing in for an open endpoint.

```python title="app.py"
from veloce import Veloce

app = Veloce()

DATA = b"x" * 5000


@app.get("/download")
async def download(request):
    spec = request.range
    if spec is None:
        return {"length": len(DATA)}
    return {"unit": spec.unit, "ranges": spec.ranges}
```

The endpoint conventions:

| Header value | `ranges` | Meaning |
| --- | --- | --- |
| `bytes=0-499` | `[(0, 499)]` | First 500 bytes. |
| `bytes=1000-` | `[(1000, None)]` | Open at the right — byte 1000 to the end. |
| `bytes=-500` | `[(None, 500)]` | Suffix range — the last 500 bytes. |
| `bytes=0-99,200-299` | `[(0, 99), (200, 299)]` | Two discrete ranges. |

`request.if_range` pairs with this: it returns `(etag, None)` when the `If-Range` value is
an ETag and `("", timestamp)` when it parses as a date, so a `Range` request can fall back
to a full `200` when the cached resource is stale.

!!! note
    Veloce's [`FileResponse`](../reference.md#veloce.FileResponse) and the
    [StaticFiles](static-files.md) mount already honour `Range` and `If-Range` for you.
    Parse `request.range` yourself only when you serve byte ranges from a custom source.

## Authorization

`request.auth` lazily parses the `Authorization:` header into an
[`Authorization`](../reference.md#veloce.Authorization), or `None` when the header is
missing or empty. `Basic` and `Bearer` are first-class; other schemes carry their
parameters in `.params`.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/whoami")
async def whoami(request):
    auth = request.auth
    if auth is None:
        return {"authenticated": False}
    if auth.type == "basic":
        return {"type": "basic", "username": auth.username}
    if auth.type == "bearer":
        return {"type": "bearer", "has_token": bool(auth.token)}
    return {"type": auth.type, "params": auth.params}
```

What each scheme populates:

| Scheme | `.type` | Populated fields |
| --- | --- | --- |
| `Basic` (RFC 7617) | `"basic"` | `.username`, `.password` (base64-decoded). |
| `Bearer` (RFC 6750) | `"bearer"` | `.token`. |
| Digest / Negotiate / custom | scheme name, lower-cased | `.params` (parsed `key="value"` pairs) or `.token`. |

`.scheme` preserves the original casing (`"Bearer"`), `.type` is always lower-cased, and
`.raw` is the unmodified header value. The raw header string is also available as
`request.authorization` if you need to handle it yourself.

!!! warning "Parsing is not verification"
    `Authorization.from_header` decodes structure only — it does not check credentials. A
    well-formed `Basic` header gives you a username and password, but you must still
    compare them (use [`constant_time_compare`](../reference.md#veloce.constant_time_compare))
    against your store. For token flows, prefer the
    [security schemes](security-schemes.md), which integrate with the OpenAPI UI.

## Testing these structures

The in-memory [`TestClient`](../reference.md#veloce.TestClient) lets you drive each path
without a server. Set request headers via `headers=` and assert on the parsed result.

```python
from veloce import JSONResponse, TestClient, Veloce

app = Veloce()


@app.get("/echo")
async def echo(request):
    return JSONResponse(
        {
            "host": request.url.host,
            "best": request.accept_mimetypes.best_match(["application/json", "text/html"]),
            "user": request.auth.username if request.auth else None,
        }
    )


client = TestClient(app)

resp = client.get(
    "/echo",
    headers={
        "Accept": "text/html, application/json;q=0.9",
        "Authorization": "Basic YWxpY2U6c2VjcmV0",  # alice:secret
    },
)
assert resp.status_code == 200
assert resp.json() == {"host": "testserver", "best": "text/html", "user": "alice"}
```

## Next steps

- Set typed and conditional response headers — see [Responses (Advanced)](responses-advanced.md).
- Read the body, query params, and cookies off the request — see [Requests and responses](requests-responses.md).
- Wire `Authorization` into a login flow — see [Security schemes](security-schemes.md).
- Full signatures are in the [API reference](../reference.md).
