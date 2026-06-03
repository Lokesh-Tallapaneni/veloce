---
description: Serve static assets in Veloce with StaticFiles — mounting, ETag and conditional GET support, range requests, directory listings, and URL generation.
tags: [static, files, assets, caching]
---

# Static files

[`StaticFiles`](../reference.md#veloce.StaticFiles) serves files from a
directory — CSS, JavaScript, images, downloads. All file I/O runs in a thread
pool so the event loop is never blocked, and responses carry `ETag`,
`Last-Modified`, and `Cache-Control` headers and honour conditional and range
requests automatically.

```bash
pip install veloceframework
```

## Mounting a static directory

The quickest way to serve a folder is [`app.mount_static`](../reference.md#veloce.Veloce.mount_static),
which constructs a `StaticFiles` handler and registers it on the app:

```python title="app.py"
from veloce import Veloce

app = Veloce()
app.mount_static(prefix="/static", directory="static")
```

With a file at `static/style.css`, a request to `/static/style.css` now returns
its contents. The `prefix` is stripped from the request path and the remainder
is resolved, traversal-safely, under `directory`. Both arguments have defaults
(`prefix="/static"`, `directory="static"`), so `app.mount_static()` with no
arguments serves `./static` at `/static`.

The directory must exist and be readable when the handler is constructed —
otherwise a typo would silently 404 every asset. By default a missing or
unreadable directory raises `ValueError` at wiring time. If you create the
directory after constructing the app (a build/deploy step), pass
`must_exist=False` to downgrade the check to a warning:

```python title="app.py"
app.mount_static(directory="build/static", must_exist=False)
```

## Using the StaticFiles class directly

You can also construct [`StaticFiles`](../reference.md#veloce.StaticFiles)
and attach it with [`app.mount`](../reference.md#veloce.Veloce.mount). When you
mount a `StaticFiles` instance, the mount prefix becomes its serving prefix:

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
app.mount("/assets", StaticFiles(directory="static"))
```

The constructor signature is `StaticFiles(directory, prefix="/static",
html=False, directory_index=False, must_exist=True, precompressed=False)`.
`directory` is required and resolved to an absolute path; it must exist and be
readable unless `must_exist=False` is passed. The other options are covered
below.

!!! note
    `mount_static` and `mount` register a static handler; they do **not** create
    a named route. Static handlers are consulted during dispatch before a
    request falls through to a 404. Because there is no named route, you build
    asset URLs as plain path strings (see [URL generation](#url-generation))
    rather than with `url_for`.

## Serving an index and HTML pages

Set `html=True` to serve `index.html` for the prefix root and to map
extensionless paths to their `.html` files — a request for `/about` will serve
`about.html` when present:

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
app.mount("/", StaticFiles(directory="public", html=True))
```

This is enough to host a small static site: `public/index.html` answers `/`, and
`public/about.html` answers both `/about.html` and `/about`.

## Directory listings

By default a request that resolves to a directory with no `index.html` is a
miss. Set `directory_index=True` to generate an HTML listing instead:

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
app.mount("/files", StaticFiles(directory="downloads", directory_index=True))
```

!!! warning
    Directory listings expose the names of every file in the folder, which is an
    information-disclosure risk. It is off by default for that reason — only
    enable it for directories whose contents are meant to be browsable. Hidden
    entries (names beginning with `.`) are always omitted from the listing. An
    entry whose target resolves outside the served root through a symlink is
    also omitted, so a listing never links to a file the server would refuse to
    serve.

## Precompressed variants

Set `precompressed=True` to serve a precompressed sibling when the client
advertises a matching `Accept-Encoding`. With `app.css` on disk alongside
`app.css.br` and/or `app.css.gz`, a request for `/static/app.css` from a client
that accepts `br` (or `gzip`) is answered with the compressed file and the
appropriate `Content-Encoding`, while the `Content-Type` stays that of the
original (`text/css`):

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
app.mount("/static", StaticFiles(directory="dist", precompressed=True))
```

The variants must be generated ahead of time (this is a serve-only feature; it
never compresses on the fly). `br` is preferred over `gzip` when the client's
quality values tie. ETag, conditional requests, and range requests all key off
the bytes actually sent. The feature is off by default because it adds one
`stat` per request to probe for the sibling.

## Caching and conditional requests

Every served file gets an `ETag` derived from its path, size, and modification
time, plus a `Last-Modified` header and `Cache-Control: public, max-age=3600`.
Veloce handles conditional requests for you:

- An [`If-None-Match`](https://developer.mozilla.org/docs/Web/HTTP/Headers/If-None-Match)
  that matches the current ETag returns `304 Not Modified` with an empty body.
- An [`If-Modified-Since`](https://developer.mozilla.org/docs/Web/HTTP/Headers/If-Modified-Since)
  no older than the file's modification time returns `304`.

This follows [RFC 9110 §13](https://www.rfc-editor.org/rfc/rfc9110#section-13):
when both headers are present, `If-None-Match` takes precedence. ETags are
cached per handler (bounded, evicting least-recently-used entries) so repeated
requests for the same file do not recompute the validator.

## Range requests

`StaticFiles` advertises `Accept-Ranges: bytes` and serves single-range
[`Range`](https://developer.mozilla.org/docs/Web/HTTP/Headers/Range) requests
with a `206 Partial Content` response — useful for resumable downloads and
media seeking. An unsatisfiable range yields `416 Range Not Satisfiable`.
Multi-range requests are not supported and fall back to the full response.

## Large files

Files at or above 1 MiB are streamed to the client in 64 KiB chunks rather than
buffered in memory, so a large download — or many concurrent ones — does not
inflate the worker's memory by the file size. Smaller files are sent as a single
response. These thresholds are class attributes you can tune by subclassing or
assigning on the instance:

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
files = StaticFiles(directory="media")
files.STREAM_THRESHOLD = 256 * 1024  # stream files >= 256 KiB
app.mount("/media", files)
```

## Security

Path resolution is traversal-safe. Requests containing `..`, absolute path
components, or NUL bytes are rejected with `403 Forbidden` before any
filesystem access. After resolving a path, Veloce dereferences symlinks and
confirms the real target is still inside the served directory, so a symlink
planted in the served tree cannot expose files elsewhere on the disk.

!!! warning
    Serve only directories whose contents you intend to make public. Do not
    point `StaticFiles` at an application source tree, a directory holding
    secrets, or a user-writable upload folder without separate validation.

## URL generation

Static handlers are not named routes, so asset URLs are ordinary path strings
built from the mount prefix. In templates, reference them directly:

```html title="templates/base.html"
<link rel="stylesheet" href="/static/style.css">
<script src="/static/app.js"></script>
```

If you prefer to centralise the prefix, register a small route that builds the
path and call it through `url_for`:

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()
app.mount_static(prefix="/static", directory="static")

STATIC_PREFIX = "/static"


def static_url(filename: str) -> str:
    return f"{STATIC_PREFIX}/{filename}"


@app.get("/")
async def index(request: Request):
    return {"stylesheet": static_url("style.css")}
```

## Serving a single file from the app's static folder

For one-off file serving, [`app.send_static_file_async`](../reference.md#veloce.Veloce.send_static_file_async)
returns a [`FileResponse`](../reference.md#veloce.FileResponse) for a file in
the app's `static_folder` (default `"static"`, resolved against the app's
package directory). It is traversal-safe via the same join logic and reads the
file in an executor, so it never blocks the event loop from an async handler:

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/favicon.ico")
async def favicon(request: Request):
    return await app.send_static_file_async("favicon.ico")
```

!!! warning
    The synchronous [`app.send_static_file`](../reference.md#veloce.Veloce.send_static_file)
    reads the file on the calling thread and emits a `DeprecationWarning` when
    invoked on a running event loop. From async handlers, use
    `app.send_static_file_async` (above) or
    [`send_from_directory_async`](../reference.md#veloce.send_from_directory_async).

`app.static_folder` and `app.static_url_path` (default `"/static"`) hold the
defaults used when wiring up static serving; assign to them before mounting to
change the conventions for your app.

## Next steps

- Render HTML that links to these assets — see [Templates](templates.md).
- Stream and download files from handlers with `FileResponse`, `send_file`, and
  the async `async_send_file` — see [Requests and responses](requests-responses.md)
  and [Flask-style helpers](helpers.md).
- Add compression and caching middleware around static responses — see
  [Middleware](middleware.md).
- Full signatures are in the [API reference](../reference.md).
