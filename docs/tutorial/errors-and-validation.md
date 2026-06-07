---
description: Tutorial step 5 — return clean error responses in Veloce with HTTPException and abort(), and customise validation error output.
tags: [tutorial, errors, validation, httpexception]
---

# 5. Errors & validation

A real API returns a `404` when a task does not exist, not a stack trace. In
Veloce you signal an error by **raising** — `HTTPException` (or the `abort()`
shortcut) — and the framework turns it into the right response.

Add a lookup route and a fetch-by-id that can fail, building on the injected
store from the previous step:

```python title="tasks.py"
from pydantic import BaseModel

from veloce import Depends, HTTPException, Veloce

app = Veloce(title="Tasks API", version="1.0.0")

_store: list[dict] = []


def get_store() -> list[dict]:
    return _store


class TaskCreate(BaseModel):
    title: str
    done: bool = False


def find_task(store: list[dict], task_id: int) -> dict:
    for task in store:
        if task["id"] == task_id:
            return task
    raise HTTPException(404, f"Task {task_id} not found")


@app.get("/tasks")
async def list_tasks(store: list[dict] = Depends(get_store)):
    return store


@app.post("/tasks")
async def create_task(task: TaskCreate, store: list[dict] = Depends(get_store)):
    record = {"id": len(store) + 1, **task.model_dump()}
    store.append(record)
    return record, 201


@app.get("/tasks/{task_id}")
async def get_task(task_id: int, store: list[dict] = Depends(get_store)):
    return find_task(store, task_id)


if __name__ == "__main__":
    app.run(port=8000)
```

Request a task that does not exist:

```bash
curl -i localhost:8000/tasks/999
```

You get a `404` with a JSON body carrying the detail and status:

```json
{"detail": "Task 999 not found", "status_code": 404}
```

## Raising errors

- `HTTPException(status_code, detail)` is the explicit form — raise it anywhere
  in a handler or dependency to short-circuit with that status and message.
- `abort(404)` is the shorthand. When you omit the detail, Veloce fills in the
  standard reason phrase (`"Not Found"`):

  ```python
  from veloce import Veloce, abort

  app = Veloce()


  @app.get("/secret")
  async def secret(token: str = ""):
      if token != "open-sesame":
          abort(403, "You shall not pass")
      return {"ok": True}
  ```

The default response renders `{"detail": ...}` with the matching status code.
That is all most apps need.

## Two kinds of validation error

There are two distinct error paths, and it helps to know which is which:

- **Request validation** (`422`) — a bad path converter, a missing required
  query value, or a body that fails its Pydantic model. Veloce raises
  `RequestValidationError` automatically *before* your handler runs.
- **Application errors** — anything you raise yourself, like the `404` above.

## Customising the error response

Register an exception handler to reshape any error. For example, wrap every
`HTTPException` in a consistent envelope:

```python title="tasks.py (excerpt)"
from veloce import HTTPException, JSONResponse, Request, Veloce

app = Veloce()


@app.exception_handler(HTTPException)
async def on_http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        {"error": exc.detail, "status": exc.status_code, "path": request.path},
        status_code=exc.status_code,
    )
```

A handler registered against a base class catches every subclass, so this one
also handles the `404` from `find_task` and the `422` from validation (which
subclasses `HTTPException`). To reshape only validation errors, register
against `RequestValidationError` instead — see the
[Error handling guide](../guide/error-handling.md).

## Next steps

The app is feature-complete. Before shipping, let's prove it works with the
in-memory test client: **[Testing](testing.md)**.
