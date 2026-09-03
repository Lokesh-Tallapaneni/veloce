---
description: Install Veloce and build your first async Python web app in 5 minutes — pip install, a minimal app, path params, and interactive API docs.
---

# Getting started

## Install

Veloce requires **Python 3.10 or newer**.

```bash
pip install veloceframework
```

That is enough to serve: the built-in `app.run()` server needs nothing else.
For an application you are deploying, the `standard` extra adds the production
ASGI server plus the two pieces whose absence is silently felt rather than
reported — `br` compression and the msgspec validation backend:

```bash
pip install "veloceframework[standard]"
```

That is [uvicorn](https://www.uvicorn.org/), `brotli` and `msgspec`. Every
other integration — Redis, gunicorn, OpenTelemetry, Prometheus, `zstd`,
`ciso8601`, Click — stays opt-in and is named where its guide introduces it;
`[all]` installs the lot.

## Your first app

Create `main.py`:

```python title="main.py"
from veloce import Veloce, Request

app = Veloce()


@app.get("/")
async def index(request: Request):
    return {"message": "Welcome to Veloce!"}


@app.get("/hello/{name}")
async def hello(name: str):
    return {"hello": name}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Run it either way:

```bash
python main.py
# or with the built-in CLI (no extra dependencies):
veloce run main:app
# or under uvicorn, the optional extra: pip install veloceframework[uvicorn]
python -m uvicorn main:app
```

Then visit [http://localhost:8000/hello/world](http://localhost:8000/hello/world)
— you should get `{"hello": "world"}`.

!!! note "`async def` is preferred, `def` is supported"

    Define handlers with `async def` for best performance — they run
    directly on the event loop. A plain `def` handler also works:
    Veloce detects it and runs it in a thread-pool executor so it never
    blocks the loop. Reach for a sync handler when calling blocking
    library code you cannot await.

## What just happened

- `Veloce()` created the application. It *is* a router, so `app.get`,
  `app.post`, and friends are available directly.
- `{name}` declared a path parameter. The `name: str` annotation tells
  Veloce how to coerce and inject it.
- Returning a `dict` produced a JSON response automatically.

## A slightly bigger app

```python
from pydantic import BaseModel

from veloce import Depends, HTTPException, Request, Veloce

app = Veloce(title="Example API", version="1.0.0")

_users: dict[int, dict] = {}


class UserCreate(BaseModel):
    name: str
    email: str
    age: int = 0


def get_db() -> dict[int, dict]:
    return _users


@app.post("/users")
async def create_user(user: UserCreate, db=Depends(get_db)):
    user_id = len(db) + 1
    db[user_id] = user.model_dump()
    return {"id": user_id, **db[user_id]}


@app.get("/users/{user_id}")
async def get_user(user_id: int, db=Depends(get_db)):
    if user_id not in db:
        raise HTTPException(404, f"User {user_id} not found")
    return {"id": user_id, **db[user_id]}
```

This shows the three pillars you will use constantly:

- **Pydantic models** as request bodies (`user: UserCreate`) — validated
  before your handler runs.
- **Dependency injection** (`Depends(get_db)`) — see
  [Dependency Injection](guide/dependency-injection.md).
- **`HTTPException`** — raise it anywhere to short-circuit with a status
  code and message.

## Interactive API docs

When OpenAPI is enabled (the default), Veloce generates an OpenAPI 3.1
schema and serves Swagger UI and ReDoc for it. With the running app, open:

- [http://localhost:8000/docs](http://localhost:8000/docs) — Swagger UI.
- [http://localhost:8000/redoc](http://localhost:8000/redoc) — ReDoc.
- [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json) —
  the raw OpenAPI 3.1 schema.

Pass `title=` and `version=` to `Veloce(...)` to control the document
metadata. The three paths are configurable through `docs_url`, `redoc_url`,
and `openapi_url`:

```python
app = Veloce(docs_url="/swagger", redoc_url="/api-docs", openapi_url="/schema.json")
```

!!! note "Disable a UI with `None`, not an empty string"
    Set `docs_url=None` or `redoc_url=None` to switch a UI off entirely.
    Veloce uses `None` as the sentinel — an empty string is not the disable
    value.

## Next steps

<div class="grid cards" markdown>

-   :material-sign-direction: **[Routing](guide/routing.md)** — path
    parameters, converters, and sub-routers.
-   :material-swap-vertical: **[Requests & Responses](guide/requests-responses.md)**
    — read input, shape output.
-   :material-needle: **[Dependency Injection](guide/dependency-injection.md)**
    — share logic across handlers.
-   :material-test-tube: **[Testing](guide/testing.md)** — drive your app
    without a network.

</div>
