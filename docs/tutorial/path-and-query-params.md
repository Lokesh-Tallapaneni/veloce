---
description: Tutorial step 2 — read typed path parameters and query-string parameters in a Veloce handler, with automatic coercion and validation.
tags: [tutorial, parameters, path, query]
---

# 2. Path & query parameters

Our Tasks API needs to look things up by id and filter lists. Both come from
the URL: the **path** (`/tasks/3`) and the **query string** (`/tasks?limit=10`).
Veloce binds them to handler arguments by name and type.

Replace the body of `tasks.py` with:

```python title="tasks.py"
from veloce import Veloce

app = Veloce(title="Tasks API", version="1.0.0")

# A throwaway list so the routes return something. We replace it with a real
# store two steps from now.
_demo_tasks = ["buy milk", "write tutorial", "ship release"]


@app.get("/")
async def index():
    return {"message": "Tasks API is running"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: int):
    return {"id": task_id, "title": _demo_tasks[task_id % len(_demo_tasks)]}


@app.get("/tasks")
async def list_tasks(limit: int = 10, q: str = ""):
    items = [t for t in _demo_tasks if q in t]
    return {"tasks": items[:limit], "limit": limit, "q": q}


if __name__ == "__main__":
    app.run(port=8000)
```

Run it and try:

- [http://localhost:8000/tasks/2](http://localhost:8000/tasks/2) →
  `{"id": 2, "title": "ship release"}`
- [http://localhost:8000/tasks?limit=2](http://localhost:8000/tasks?limit=2) →
  the first two tasks
- [http://localhost:8000/tasks?q=write](http://localhost:8000/tasks?q=write) →
  only matching tasks

## Path parameters

`{task_id}` in the route declares a path parameter. The `task_id: int`
annotation tells Veloce to coerce the URL segment to an integer **before** your
handler runs. Request `/tasks/abc` and you get a `422` response — `"abc"` is
not an `int` — without writing any validation yourself.

## Query parameters

`limit` and `q` are **not** in the path, so Veloce reads them from the query
string. The rule is: an argument whose name matches a path placeholder is a
path parameter; otherwise it is read from the query string. Giving each a
default (`limit: int = 10`, `q: str = ""`) makes it optional, so `/tasks` works
with no query at all.

Query values are coerced too: `?limit=5` arrives as the integer `5`, not the
string `"5"`.

## Adding constraints

When you need validation — a minimum page, a maximum length — use the
[`Query`](../guide/parameters.md) and [`Path`](../guide/parameters.md) marker
classes as the default:

```python title="tasks.py"
from veloce import Path, Query, Veloce

app = Veloce(title="Tasks API", version="1.0.0")

_demo_tasks = ["buy milk", "write tutorial", "ship release"]


@app.get("/tasks/{task_id}")
async def get_task(task_id: int = Path(ge=0, description="Task id")):
    return {"id": task_id, "title": _demo_tasks[task_id % len(_demo_tasks)]}


@app.get("/tasks")
async def list_tasks(limit: int = Query(default=10, ge=1, le=100), q: str = ""):
    items = [t for t in _demo_tasks if q in t]
    return {"tasks": items[:limit], "limit": limit, "q": q}
```

Now `/tasks?limit=0` is rejected with a `422` because of `ge=1`, and the
constraints show up in the OpenAPI schema. See the
[Parameters guide](../guide/parameters.md) for the full set of markers
(`Header`, `Cookie`, `Form`, `File`) and constraints.

## Next steps

Reading the URL is enough for lookups, but creating a task needs a request
**body**. Next: **[Request bodies](request-body.md)**.
