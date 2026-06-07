---
description: Tutorial step 3 — accept and validate a JSON request body in Veloce with a Pydantic model, and return the created resource.
tags: [tutorial, request-body, pydantic, validation]
---

# 3. Request bodies

To create a task the client sends JSON. Declare a **Pydantic model** and
annotate a handler parameter with it: Veloce reads the request body, validates
it against the model, and hands your handler a fully-typed object.

Update `tasks.py` to add a `Task` model and a `POST` handler:

```python title="tasks.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce(title="Tasks API", version="1.0.0")

# Still a throwaway store — we wire in dependency injection next step.
_tasks: list[dict] = []


class TaskCreate(BaseModel):
    title: str
    done: bool = False


@app.get("/tasks")
async def list_tasks():
    return _tasks


@app.post("/tasks")
async def create_task(task: TaskCreate):
    record = {"id": len(_tasks) + 1, **task.model_dump()}
    _tasks.append(record)
    return record, 201


if __name__ == "__main__":
    app.run(port=8000)
```

Run it and create a task:

```bash
curl -X POST localhost:8000/tasks \
    -H "Content-Type: application/json" \
    -d '{"title": "write the tutorial"}'
```

You get back the created record with a `201 Created` status:

```json
{"id": 1, "title": "write the tutorial", "done": false}
```

## What just happened

- `TaskCreate` is an ordinary Pydantic `BaseModel`. Because the `task`
  parameter is annotated with it, Veloce reads the JSON body and validates it.
- `done: bool = False` is optional with a default, so the client may omit it.
- `task.model_dump()` turns the validated model back into a plain dict to
  store.
- Returning `record, 201` is the **(body, status)** tuple shorthand — Veloce
  serialises `record` as JSON and sets the status to `201`. A bare `return
  record` would default to `200`.

## Validation comes for free

The model defines the contract, so bad input is rejected before your code
runs. Send a body with the wrong type:

```bash
curl -X POST localhost:8000/tasks \
    -H "Content-Type: application/json" \
    -d '{"title": 123, "done": "maybe"}'
```

Veloce responds with a `422` and a structured error body pointing at each bad
field — you never wrote a single `if`:

```json
{"detail": [{"loc": ["body", "done"], "msg": "...", "type": "..."}]}
```

A missing required field (`title`) is rejected the same way.

!!! tip "Separate input and output models"
    A common pattern is one model for the **incoming** body (`TaskCreate`) and
    another for the **response**, so you never accidentally echo internal
    fields. Declare the response shape with `response_model=` on the route when
    you want Veloce to enforce and document it.

## Next steps

The `_tasks` list is a module global — fine for one file, awkward to test or
swap. Next we inject it as a **dependency**:
**[Dependencies](dependencies.md)**.
