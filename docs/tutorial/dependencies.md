---
description: Tutorial step 4 — share a data store across Veloce handlers with Depends(), and learn why dependency injection makes the app testable.
tags: [tutorial, dependency-injection, depends]
---

# 4. Dependencies

Right now every handler reaches for the module-level `_tasks` list directly.
That works, but it hard-wires the store into each handler — there is no way to
swap it for a fake in a test, or for a real database later. **Dependency
injection** fixes that: a handler declares what it needs, and Veloce provides
it.

Refactor `tasks.py` so the store comes from a dependency:

```python title="tasks.py"
from pydantic import BaseModel

from veloce import Depends, Veloce

app = Veloce(title="Tasks API", version="1.0.0")

_store: list[dict] = []


def get_store() -> list[dict]:
    return _store


class TaskCreate(BaseModel):
    title: str
    done: bool = False


@app.get("/tasks")
async def list_tasks(store: list[dict] = Depends(get_store)):
    return store


@app.post("/tasks")
async def create_task(task: TaskCreate, store: list[dict] = Depends(get_store)):
    record = {"id": len(store) + 1, **task.model_dump()}
    store.append(record)
    return record, 201


if __name__ == "__main__":
    app.run(port=8000)
```

The behaviour is identical, but no handler mentions `_store` directly — they
ask for it via `Depends(get_store)`.

## How `Depends` works

- A dependency is **any callable**. `get_store` just returns the list, but a
  dependency can do real work — open a connection, parse a header, look up the
  current user.
- `Depends(get_store)` as a parameter default tells Veloce: "call this and pass
  the result as `store`." The dependency runs once per request, and its result
  is cached for the rest of that request.
- Dependencies can be sync or `async`, and they can themselves depend on other
  dependencies (or on the `Request`). Veloce resolves the whole chain from a
  plan compiled once at registration — there is no per-request reflection.

## Dependencies that need the request

A dependency can take a `Request` and read headers, the same way a handler can.
Here is a tiny auth gate that other handlers can reuse:

```python title="tasks.py (excerpt)"
from veloce import Depends, HTTPException, Request, Veloce

app = Veloce()


async def require_token(request: Request) -> str:
    token = request.headers.get("authorization", "")
    if token != "Bearer let-me-in":
        raise HTTPException(401, "Not authenticated")
    return token


@app.delete("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def delete_task(task_id: int):
    return {"deleted": task_id}
```

Because `require_token` is only needed for its **side effect** (the check), it
is attached with `dependencies=[...]` on the route rather than as a parameter —
its return value is not used. The same `dependencies=` argument works on a
`Router` (applying to every route it holds) and on `Veloce(...)` for app-wide
checks.

## Why this matters for testing

Because the store is injected, a test can replace it without touching the real
one using `app.dependency_overrides` — which is exactly what the next step
does. See the [Dependency Injection guide](../guide/dependency-injection.md)
for `yield` teardown, scopes, and security schemes.

## Next steps

Our handlers happily index a missing task. Next we return proper error
responses: **[Errors & validation](errors-and-validation.md)**.
