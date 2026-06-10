---
description: Receive multipart/form-data uploads with UploadFile and FormData, read and save uploaded files, and bind form fields and files as handler parameters.
tags: [uploads, multipart, UploadFile, FormData]
---

# File uploads

Veloce parses `multipart/form-data` and `application/x-www-form-urlencoded`
bodies on demand. Uploaded files arrive as [`UploadFile`](../reference.md#veloce.UploadFile)
objects with an async read/write interface; text fields and files together
live in a [`FormData`](../reference.md#veloce.FormData) multi-value mapping.

The multipart parser ships with Veloce. There is no `python-multipart`
dependency to install — form and file handling works out of the box.

```python
from veloce import Request, Veloce

app = Veloce()


@app.post("/upload")
async def upload(request: Request):
    form = await request.form()
    upload = form.get_upload("file")
    if upload is None:
        return {"error": "no file field named 'file'"}, 400
    data = await upload.read()
    return {"filename": upload.filename, "size": len(data)}
```

`request.form()` is an async method — always `await` it. It returns a
`FormData` containing both text fields and uploads. `get_upload("file")`
returns the first value for `file` if it is an `UploadFile`, otherwise
`None`.

## Reading the form

`request.form()` parses the body once and caches the result, so calling it
more than once in the same handler is cheap. The mapping is a
`multidict.MultiDict` subclass: single-value access returns the first
value, and `getlist` returns every value for a repeated field.

```python
from veloce import Request, Veloce

app = Veloce()


@app.post("/profile")
async def profile(request: Request):
    form = await request.form()
    name = form.get("name")            # first value, or None
    tags = form.getlist("tag")         # every value for repeated `tag`
    return {"name": name, "tags": tags}
```

For a `multipart/form-data` body, file parts become `UploadFile` values
and text parts become plain strings. For an
`application/x-www-form-urlencoded` body, every value is a string. For any
other content type, `request.form()` returns an empty `FormData`.

!!! note
    `request.form()` and `request.files()` are coroutines. Forgetting the
    `await` gives you the coroutine object, not the data — a common
    mistake when porting from synchronous frameworks.

## Files only

When a handler only cares about uploads, `request.files()` returns a
`FormData` containing just the entries whose value is an `UploadFile`.
Text fields are excluded. Like `request.form()`, it is async and cached.

```python
from veloce import Request, Veloce

app = Veloce()


@app.post("/attachments")
async def attachments(request: Request):
    files = await request.files()
    return {"count": len(files), "names": [f.filename for f in files.values()]}
```

!!! tip "Debug-mode diagnostics"

    The most common upload mistake is submitting the form without
    `enctype="multipart/form-data"`, so the field arrives as plain text and is
    absent from `request.files()`. When the application runs with `debug=True`,
    a missing-key lookup such as `files["avatar"]` raises
    [`FilesKeyError`](../reference.md#veloce.FilesKeyError) — a `KeyError`
    subclass whose message names the cause (missing `enctype`, a JSON body, or
    no multipart body at all). In production the lookup raises a plain
    `KeyError`. Use `files.get("avatar")` or `files.get_upload("avatar")` to
    avoid the exception entirely.

## The UploadFile object

[`UploadFile`](../reference.md#veloce.UploadFile) wraps the uploaded
bytes behind an async interface. Small uploads stay in memory; larger
ones are spooled to a temporary file by the multipart parser, and the
read/write methods transparently hop to a thread when the spool has
rolled over to disk.

The object exposes:

- `filename` — the client-supplied file name.
- `content_type` — the part's declared content type, defaulting to
  `"application/octet-stream"`.
- `size` — the part size in bytes.
- `headers` — the part headers as a case-insensitive `Headers` object.
- `read(size=-1)` — async; read up to `size` bytes, or the whole file
  when `size` is `-1`.
- `write(data)` — async; append bytes.
- `seek(offset)` — async; move the read cursor.
- `close()` — async; close the underlying spool.
- `save(destination, buffer_size=16384)` — sync; stream the upload to a
  path or open binary file object.
- `content` — sync property returning the full content as bytes.

`UploadFile` is also an async context manager — leaving the `async with`
block closes the underlying file:

```python
from veloce import Request, Veloce

app = Veloce()


@app.post("/echo")
async def echo(request: Request):
    form = await request.form()
    upload = form.get_upload("file")
    if upload is None:
        return {"error": "missing file"}, 400
    async with upload:
        head = await upload.read(16)
    return {"filename": upload.filename, "head": head.decode("latin-1")}
```

## Saving an upload to disk

`save` streams the upload in fixed-size chunks so memory stays bounded
for large files. Pass a filesystem path (opened in `"wb"` mode and closed
for you) or an already-open binary file object (which you remain
responsible for closing). The read cursor is reset to the start before
streaming and restored afterwards, so the upload stays readable.

```python
from pathlib import Path

from veloce import Request, Veloce, secure_filename

app = Veloce()
UPLOAD_DIR = Path("uploads")


@app.post("/save")
async def save(request: Request):
    form = await request.form()
    upload = form.get_upload("file")
    if upload is None:
        return {"error": "missing file"}, 400
    UPLOAD_DIR.mkdir(exist_ok=True)
    name = secure_filename(upload.filename)
    upload.save(str(UPLOAD_DIR / name))
    return {"saved": name}
```

!!! warning "Sanitise file names"
    Never trust `upload.filename` as a path component. A malicious client
    can send `../../etc/passwd`. Run it through
    [`secure_filename`](../reference.md#veloce.secure_filename) (or build the
    destination from a server-controlled name) before writing to disk.
    See the [OWASP file upload guidance](https://owasp.org/www-community/vulnerabilities/Unrestricted_File_Upload).

!!! note
    `save` and the `content` property are synchronous. For an upload that
    has been spooled to disk, `content` performs blocking I/O on the
    event loop; prefer `await upload.read()` in that case. `save` streams
    in chunks and is fine for large files, but it still runs to
    completion inline — offload it with `run_in_executor` if you need the
    loop free during a very large write.

## Files as handler parameters

Instead of reaching into `request.form()` by hand, declare the upload as
a parameter. Annotating a parameter as `UploadFile` binds it from the
multipart form by parameter name. Use [`File`](../reference.md#veloce.File)
for an explicit marker and [`Form`](../reference.md#veloce.Form) for text
fields submitted alongside it.

```python
from veloce import File, Form, UploadFile, Veloce

app = Veloce()


@app.post("/items")
async def create_item(
    name: str = Form(),
    file: UploadFile = File(),
):
    data = await file.read()
    return {"name": name, "filename": file.filename, "size": len(data)}
```

A bare `UploadFile` annotation works too — the parameter is bound from
the form by name. Make it optional with `UploadFile | None = None` so a
request without that part still resolves:

```python
from veloce import UploadFile, Veloce

app = Veloce()


@app.post("/avatar")
async def avatar(avatar: UploadFile | None = None):
    if avatar is None:
        return {"uploaded": False}
    return {"uploaded": True, "filename": avatar.filename}
```

See [Parameters](parameters.md) for the full set of declarative
markers and their shared validation options.

!!! warning "Form and a body model cannot share one handler"
    A `Form()`/`File()`/`UploadFile` parameter reads the body as
    `multipart/form-data` or `application/x-www-form-urlencoded`; a Pydantic
    (or `msgspec`) body model reads it as JSON. A single request body has one
    content type, so the two are mutually exclusive on the same handler.
    Mixing them does not error at registration — at runtime the form parser
    returns an empty `FormData` for a JSON body (and JSON parsing fails on a
    form body), so one of the two parameters resolves as missing. Split the
    endpoint, or send the structured fields as individual `Form()` fields.

### Shapes Veloce does not bind

Two FastAPI-style shapes are accepted by the type system but are **not**
wired to multipart binding in Veloce. They are listed here so the divergence
is explicit.

| Shape | What FastAPI does | What Veloce does |
| --- | --- | --- |
| `data: bytes = File()` | Reads the part's raw bytes into `data`. | Binds the `UploadFile` object, then tries to coerce it to `bytes` — it does not read the content. Use `UploadFile` and `await upload.read()`. |
| `files: list[UploadFile]` | Binds every part sharing the field name. | Falls through to query-list binding, not multipart. Read repeated uploads with `(await request.files()).getlist("field")`. |

!!! note
    To accept many files under one field name, reach into the form directly:

    ```python
    from veloce import Request, Veloce

    app = Veloce()


    @app.post("/gallery")
    async def gallery(request: Request):
        files = await request.files()
        uploads = files.getlist("photos")
        return {"count": len(uploads)}
    ```

## Limits

The multipart parser caps the number of parts and the size of each part
to bound memory and CPU under malicious input. The defaults are 1000
parts and 10 MiB per part. Override them per application through
`app.config`:

```python
from veloce import Veloce

app = Veloce()
app.config["MAX_FORM_PARTS"] = 200
app.config["MAX_FORM_PART_SIZE"] = 2 * 1024 * 1024   # 2 MiB per part
```

File and text parts can be limited independently. This is the common
"small fields, large files" policy: cap each text field tightly while
allowing larger uploads, and bound the file and field counts separately.

```python
app.config["MAX_FORM_FIELD_SIZE"] = 64 * 1024          # 64 KiB per text field
app.config["MAX_FORM_FILE_SIZE"] = 50 * 1024 * 1024    # 50 MiB per file
app.config["MAX_FORM_FIELDS"] = 500                    # at most 500 text fields
app.config["MAX_FORM_FILES"] = 10                      # at most 10 files
app.config["MAX_FORM_FIELD_MEMORY"] = 1024 * 1024      # 1 MiB of text in total
```

`MAX_FORM_FIELD_MEMORY` caps the combined resident size of every text
field (value bytes plus field-name bytes), a ceiling that the per-field
limit alone cannot express.

When a request exceeds any of these limits, parsing fails with
`413 Request Entity Too Large` before the whole body is buffered.

A multipart request with a missing or malformed boundary is rejected
with `400 Bad Request` rather than parsed to an empty form.

A text field that declares its own `Content-Type` charset (one of
`ascii`, `us-ascii`, `utf-8`, or `iso-8859-1`) is decoded with that
charset.

See [Configuration](configuration.md) for how `app.config` is loaded and
overridden.

## Testing uploads

The in-memory [`TestClient`](testing.md) sends multipart bodies through the
`files` argument. Each entry may be raw `bytes`, a `str`, a file-like object,
a `(filename, content)` tuple, or a `(filename, content, content_type)`
tuple — the same shapes `requests` and `httpx` accept. Pass plain text fields
through `data`.

```python
from veloce import File, Form, TestClient, UploadFile, Veloce

app = Veloce()


@app.post("/items")
async def create_item(name: str = Form(), file: UploadFile = File()):
    data = await file.read()
    return {"name": name, "filename": file.filename, "size": len(data)}


client = TestClient(app)

response = client.post(
    "/items",
    data={"name": "report"},
    files={"file": ("report.txt", b"hello world")},
)
assert response.status_code == 200
assert response.json() == {
    "name": "report",
    "filename": "report.txt",
    "size": 11,
}
```

## Next steps

- [Parameters](parameters.md) — `Form`, `File`, and the other declarative
  markers.
- [Requests and responses](requests-responses.md) — the rest of the
  `Request` API and the response shapes a handler can return.
- [Testing](testing.md) — driving the app with `TestClient`.
- API reference: [`UploadFile`](../reference.md#veloce.UploadFile),
  [`FormData`](../reference.md#veloce.FormData).
