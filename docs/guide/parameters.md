---
description: Declare typed request parameters in Veloce with Query, Path, Body, Form, File, Header, and Cookie — aliases, defaults, numeric and string constraints, and OpenAPI metadata.
tags: [parameters, query, path, body, form, validation]
---

# Parameters

Veloce binds handler arguments to parts of the request by name and type.
When the defaults are not enough, the parameter marker classes —
[`Query`](../reference/parameters.md#veloce.Query), [`Path`](../reference/parameters.md#veloce.Path),
[`Body`](../reference/parameters.md#veloce.Body), [`Form`](../reference/parameters.md#veloce.Form),
[`File`](../reference/parameters.md#veloce.File), [`Header`](../reference/parameters.md#veloce.Header),
and [`Cookie`](../reference/parameters.md#veloce.Cookie) — let you set an alias, a
default, validation constraints, and OpenAPI metadata.

```python title="app.py"
from veloce import Query, Request, Veloce

app = Veloce()


@app.get("/search")
async def search(request: Request, q: str = Query(default=""), page: int = Query(default=1, ge=1)):
    return {"q": q, "page": page}
```

`/search?q=veloce&page=2` yields `q="veloce"`, `page=2`. A request with
`page=0` is rejected with a `422` because of the `ge=1` constraint.

## How binding works without markers

You do not need a marker for the common case. A handler parameter is bound by
these rules, in order: a name that matches a path segment is a path
parameter; a `Request` annotation receives the request; a Pydantic model is
read from the body; anything else is read from the query string. A default
value makes the parameter optional. See [Routing](routing.md#query-parameters)
for the marker-free form.

Reach for a marker when you need to rename a parameter, validate it, mark it
optional with constraints, or document it in the OpenAPI schema.

## Shared constructor

All seven classes share the same constructor. The first argument, `default`,
defaults to the `Ellipsis` sentinel (`...`), which means the parameter is
**required**. Supply any other value to make it optional.

The most useful keyword arguments are:

| Argument | Meaning |
|----------|---------|
| `default` | Value used when the parameter is absent; omit it to make the parameter required. |
| `default_factory` | A zero-argument callable invoked on every request the parameter is absent, so each request gets its own value. Use it for mutable defaults (`default_factory=list`) instead of a shared `default=[]`. Mutually exclusive with `default`. |
| `alias` | The wire name to read from, when it differs from the Python argument name. |
| `title`, `description` | OpenAPI documentation strings. |
| `ge`, `le`, `gt`, `lt` | Numeric bounds for `int` / `float` / `Decimal` values. |
| `min_length`, `max_length` | Length bounds for `str` values. |
| `multiple_of` | Require a numeric value to be a multiple of this (must be positive). |
| `pattern` | A regular expression the full string value must match. |
| `deprecated` | Mark the parameter deprecated in the schema. |
| `examples` | A list of example values for the schema. |
| `include_in_schema` | When `False`, the parameter is still resolved but omitted from the OpenAPI `parameters` list. |

!!! note
    `pattern` is the current name for the regex constraint; the older `regex`
    keyword is still accepted, and `pattern` wins if you pass both. Passing a
    non-positive `multiple_of` raises `ValueError` at startup rather than
    producing a non-conformant schema.

!!! warning "Mutable defaults"
    A static mutable default — `Query(default=[])` — is constructed once and
    shared by every request, so an in-place mutation by one handler leaks into
    the next. Veloce warns at startup when it sees a `list`, `dict`, or `set`
    static default and points you at `default_factory`, which builds a fresh
    value per request:

    ```python
    async def search(tags: list[str] = Query(default_factory=list)):
        ...
    ```

## Query

[`Query`](../reference/parameters.md#veloce.Query) reads from the URL query string. Use
`alias` to bind a wire name that is not a valid Python identifier, and the
constraint arguments to validate the value.

```python
from veloce import Query, Request, Veloce

app = Veloce()


@app.get("/items")
async def list_items(
    request: Request,
    q: str = Query(default="", max_length=50, description="Search text"),
    page: int = Query(default=1, ge=1),
):
    return {"q": q, "page": page}
```

A list-typed parameter collects repeated query keys. Annotate it with
`list[...]`:

```python
from veloce import Request, Veloce

app = Veloce()


@app.get("/tags")
async def by_tags(request: Request, tag: list[str] = []):
    return {"tags": tag}   # /tags?tag=a&tag=b -> ["a", "b"]
```

A `bool`-annotated query, path, header, or cookie parameter is coerced from
the raw string.

The value is true only when it lower-cases to one of `true`,
`1`, or `yes`; every other string (including `false`, `0`, `no`, `off`, and the
empty string) coerces to `False`.

```python
from veloce import Request, Veloce

app = Veloce()


@app.get("/items")
async def list_items(request: Request, archived: bool = False):
    return {"archived": archived}   # /items?archived=yes -> True
```

!!! warning "Bool coercion accepts a fixed token set"
    Unlike FastAPI, which treats `on`/`off` as valid bool tokens, Veloce only
    recognises `true`, `1`, and `yes` (case-insensitively) as `True`.

    Anything else is `False` — there is no `422` for an unrecognised value, so a
    typo like `archived=ture` silently reads as `False`.

    The same token set applies to `Query`, `Path`, `Header`, and `Cookie`
    parameters.

## Path

[`Path`](../reference/parameters.md#veloce.Path) annotates a value taken from a path
segment. The argument name must match the `{...}` placeholder in the route.
Path parameters are always required, so a `default` is not meaningful, but the
constraints and documentation arguments still apply.

```python
from veloce import Path, Veloce

app = Veloce()


@app.get("/items/{item_id}")
async def get_item(item_id: int = Path(ge=1, description="Item ID")):
    return {"id": item_id}
```

## Body

[`Body`](../reference/parameters.md#veloce.Body) binds the raw request body, or a named
field within a JSON body when `embed=True`. For structured payloads, prefer a
Pydantic model (see [Requests & responses](requests-responses.md)); use
`Body` for a single scalar value.

```python
from veloce import Body, Veloce

app = Veloce()


@app.post("/echo")
async def echo(message: str = Body(embed=True)):
    # expects {"message": "..."} in the JSON body
    return {"message": message}
```

The `embed` argument is only meaningful for `Body`. Without it the whole body
is treated as the value.

## Form

[`Form`](../reference/parameters.md#veloce.Form) reads a field from an
`application/x-www-form-urlencoded` or `multipart/form-data` body.

```python
from veloce import Form, Veloce

app = Veloce()


@app.post("/login")
async def login(username: str = Form(), password: str = Form(min_length=8)):
    return {"user": username}
```

## File

[`File`](../reference/parameters.md#veloce.File) declares an uploaded file field in a
`multipart/form-data` body. To receive the upload itself, annotate the
argument with [`UploadFile`](../reference/requests.md#veloce.UploadFile).

```python
from veloce import File, UploadFile, Veloce

app = Veloce()


@app.post("/upload")
async def upload(document: UploadFile = File(description="The file to store")):
    contents = await document.read()
    return {"filename": document.filename, "size": len(contents)}
```

See [File uploads](file-uploads.md) for the full `UploadFile` API.

## Header

[`Header`](../reference/parameters.md#veloce.Header) reads an HTTP request header. By
default an un-aliased header argument has its underscores rewritten to
hyphens, so `x_token` reads the `X-Token` header. Set
`convert_underscores=False` to disable that, or pass an explicit `alias`.

```python
from veloce import Header, Request, Veloce

app = Veloce()


@app.get("/whoami")
async def whoami(request: Request, x_token: str = Header(default="")):
    return {"token": x_token}   # reads the X-Token header
```

Header names are case-insensitive per [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#name-field-names).
When you supply `alias=`, the alias is used verbatim and underscore
conversion does not apply.

## Cookie

[`Cookie`](../reference/parameters.md#veloce.Cookie) reads a single cookie value.

```python
from veloce import Cookie, Request, Veloce

app = Veloce()


@app.get("/session")
async def read_session(request: Request, session_id: str = Cookie(default=None)):
    return {"session_id": session_id}
```

## Grouping a model over one source

A model annotation under a marker means **one key holding a JSON document** —
`?f={"token":"abc"}`. Pass `group=True` to read the model's **fields** from
that source instead, so a shared filter or pagination model is declared once
and reused:

```python
from pydantic import BaseModel

from veloce import Query, Veloce

app = Veloce()


class Filters(BaseModel):
    token: str
    skip: int = 0
    limit: int = 100


@app.get("/items")
async def read_items(filters: Filters = Query(group=True)):
    return {"skip": filters.skip, "limit": filters.limit}
```

`GET /items?token=abc&skip=5&limit=10` fills each field, and absent fields fall
back to the model's own defaults. Validation errors name the offending field,
not the group — a bad `limit` reports `["query", "limit"]`.

`group=True` works on `Query`, `Header`, `Cookie`, and `Form`. Field aliases are
honoured (`Field(alias="pageSize")` reads `?pageSize=`), a `Header` field's
underscores become hyphens (`x_token` reads `x-token`), and a `list`-typed field
collects repeated keys.

!!! note "Added in version 0.15"
    `group=True`. Without it a model annotation keeps its existing meaning of a
    single JSON-document key, so existing handlers are unaffected.

!!! warning "Grouped parameters skip the compiled resolver"
    A grouped model resolves through the interpreted path rather than the
    compiled per-route resolver, which measures slower than declaring the same
    fields as individual parameters. Prefer loose parameters on hot routes;
    reach for `group=True` when sharing one model across many handlers is worth
    more than the per-request difference.

## Declaring markers with Annotated

Following [PEP 593](https://peps.python.org/pep-0593/), you can attach a
marker through `Annotated` instead of the default value. This keeps the
argument's real default free for an ordinary value.

```python
from typing import Annotated

from veloce import Query, Request, Veloce

app = Veloce()


@app.get("/search")
async def search(request: Request, q: Annotated[str, Query(max_length=50)] = ""):
    return {"q": q}
```

The marker in the `Annotated` metadata supplies the constraints; the value
after `=` supplies the default. If you set the marker as the default *and* in
`Annotated`, the default position wins.

## Validation errors

When a value violates a constraint or a required parameter is missing, Veloce
responds with a `422` and a structured error body.

The same validation rules apply across all seven marker types: numeric bounds
(`ge`, `le`, `gt`, `lt`, `multiple_of`) for numbers, and `min_length` /
`max_length` / `pattern` for strings.

See [Error handling](error-handling.md) for customising the response.

## Next steps

- [Routing](routing.md) — path converters and marker-free parameter binding.
- [Requests & responses](requests-responses.md) — Pydantic body models.
- [File uploads](file-uploads.md) — `UploadFile` and multipart forms.
- [Dependency injection](dependency-injection.md) — share parameter parsing
  across handlers with `Depends`.
- [API reference](../reference/index.md) — `Query`, `Path`, `Body`, `Form`, `File`,
  `Header`, `Cookie`.
