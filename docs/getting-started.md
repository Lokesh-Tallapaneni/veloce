# Getting started

## Install

Veloce requires **Python 3.10 or newer**.

```bash
pip install veloce
```

To run an app you will also want an ASGI server such as
[uvicorn](https://www.uvicorn.org/):

```bash
pip install uvicorn
```

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
# or, equivalently
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
schema and serves Swagger UI and ReDoc for it. Pass `title=` and
`version=` to `Veloce(...)` to control the document metadata.

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
