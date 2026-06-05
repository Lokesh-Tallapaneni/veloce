---
description: Read request bodies, headers, cookies, and form data; return JSON, HTML, file, redirect, or streaming responses with Pydantic models and response_model shaping.
tags: [request, response, json, pydantic]
---

# Requests & Responses

## The Request object

Annotate a parameter as `Request` to receive the incoming request:

```python
from veloce import Request, Veloce

app = Veloce()


@app.get("/inspect")
async def inspect(request: Request):
    return {
        "method": request.method,
        "path": request.path,
        "query": dict(request.query_params),
        "user_agent": request.headers.get("user-agent"),
    }
```

`request.headers` and `request.query_params` are multi-value mappings:
`headers["x"]` returns the first value, `headers.getlist("x")` returns
all of them.

## Returning responses

A handler can return several shapes — Veloce coerces each to a response.

### Dict shortcut (JSON)

Return a `dict` (or any JSON-serialisable value) and Veloce wraps it in a
JSON response with a `200` status:

```python
@app.get("/dict")
async def as_dict(request: Request):
    return {"json": True}            # -> JSON response
```

### Tuple shortcut (body, status, headers)

Return a tuple to set an explicit status code — and, optionally, a
headers mapping — alongside the body:

```python
@app.get("/tuple")
async def as_tuple(request: Request):
    return {"created": True}, 201    # -> JSON with an explicit status
```

### Response classes

For explicit control over the content type and status, return a response
object:

```python
from veloce import HTMLResponse, JSONResponse


@app.get("/html")
async def html(request: Request):
    return HTMLResponse("<h1>Hello from Veloce</h1>")


@app.get("/json")
async def json(request: Request):
    return JSONResponse({"ok": True}, status_code=200)
```

Other response classes include `PlainTextResponse`, `RedirectResponse`,
`FileResponse`, and `ORJSONResponse` / `UJSONResponse`.

### Streaming responses

For large or open-ended payloads, return a `StreamingResponse` over an
async iterator — Veloce sends each chunk as soon as it's produced,
without buffering the whole body in memory:

```python
from veloce import StreamingResponse


async def numbers():
    for i in range(1000):
        yield f"{i}\n"


@app.get("/stream")
async def stream(request: Request):
    return StreamingResponse(numbers(), content_type="text/plain")
```

## Request bodies with Pydantic

Annotate a parameter with a Pydantic model — Veloce parses and validates
the body before the handler runs. Invalid input produces a structured
`422` automatically:

```python
from pydantic import BaseModel


class UserCreate(BaseModel):
    name: str
    email: str
    age: int = 0


@app.post("/users")
async def create_user(user: UserCreate):
    return {"stored": user.model_dump()}
```

## Shaping output with `response_model`

`response_model` runs the return value through a model on the way out —
useful for trimming internal fields:

```python
class UserPublic(BaseModel):
    id: int
    name: str


@app.get("/users/{user_id}", response_model=UserPublic)
async def get_user(user_id: int):
    # extra keys like "password_hash" are dropped from the response
    return {"id": user_id, "name": "ada", "password_hash": "..."}
```

## Errors

Raise `HTTPException` anywhere to stop with a status code and message:

```python
from veloce import HTTPException


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id != 1:
        raise HTTPException(404, f"User {user_id} not found")
    return {"id": user_id}
```

Register a custom handler to control the error payload:

```python
from veloce import JSONResponse


@app.exception_handler(HTTPException)
async def on_http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        {"error": exc.detail, "path": request.path},
        status_code=exc.status_code,
    )
```

## See also

- [Routing](routing.md)
- [Dependency injection](dependency-injection.md)
- [Testing](testing.md)
