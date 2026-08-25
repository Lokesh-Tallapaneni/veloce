---
description: Tutorial step 6 — test a Veloce app with the in-memory TestClient, override dependencies with fakes, and assert on JSON responses and status codes.
tags: [tutorial, testing, testclient, pytest]
---

# 6. Testing

Veloce ships an in-memory `TestClient`. It builds ASGI scopes directly and runs
your handlers through the **real** application surface — middleware, response
encoding, cookies, and the lifespan handshake all execute — with no socket and
no separate server. Tests run in-process and fast.

## The finished app

Here is the complete Tasks API from the previous steps. Save it as `tasks.py`:

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

## A first test

Save this as `test_tasks.py` next to `tasks.py`:

```python title="test_tasks.py"
import pytest

from tasks import app, get_store


@pytest.fixture
def client():
    # Override the store with a fresh list per test so each test is isolated.
    # The override returns the *same* list on every call within the test, so
    # state created by one request is visible to the next.
    store: list[dict] = []
    app.dependency_overrides[get_store] = lambda: store
    yield app.test_client()
    app.dependency_overrides.clear()


def test_create_and_fetch(client):
    created = client.post("/tasks", json={"title": "write tests"})
    assert created.status_code == 201
    assert created.json() == {"id": 1, "title": "write tests", "done": False}

    listed = client.get("/tasks")
    assert listed.status_code == 200
    assert listed.json() == [{"id": 1, "title": "write tests", "done": False}]


def test_missing_task_is_404(client):
    response = client.get("/tasks/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Task 999 not found"


def test_invalid_body_is_422(client):
    response = client.post("/tasks", json={"title": 123, "done": "maybe"})
    assert response.status_code == 422
```

Run it:

```bash
pip install pytest
pytest
```

All three tests pass.

## What's going on

- `app.test_client()` returns a `TestClient` bound to your app. It exposes the
  HTTP verbs (`get`, `post`, `put`, `patch`, `delete`) and returns a response
  with `status_code`, `json()`, `text`, and `headers`.
- `json=...` sends a JSON body; query parameters go through `params=...`.
- `app.dependency_overrides[get_store] = lambda: store` swaps the real store
  for a per-test list, so each test starts empty and never touches the
  module-level `_store`. The lambda returns the *same* list on each call within
  a test, so a `POST` is visible to a following `GET`. This is the payoff from
  [step 4](dependencies.md) — injection makes the store replaceable.
- The `client` fixture clears the overrides on teardown
  (`app.dependency_overrides.clear()`), so one test's fake never leaks into the
  next. `dependency_overrides` is just a plain dict on the app.

## Lifespan, WebSockets, and more

Use the client as a context manager to run `startup`/`shutdown` events around
a block, and `websocket_connect` to drive a `@app.websocket` route — both
in-memory:

```python
def test_with_lifespan():
    with app.test_client() as client:
        # startup handlers have run here
        assert client.get("/tasks").status_code == 200
    # shutdown handlers have run here
```

See the [Testing guide](../guide/testing.md) for streaming request bodies,
cookie persistence, and the async client.

## Where to go next

You have built and tested a complete Veloce service. From here:

<div class="grid cards" markdown>

-   :material-book-open-variant: **[The guides](../guide/routing.md)** — every
    feature in depth, from middleware to sessions to WebSockets.
-   :material-code-braces: **[Scaffold a project](../guide/cli.md)** — `veloce new`
    generates a runnable app you can build on.
-   :material-needle: **[Dependency Injection](../guide/dependency-injection.md)**
    — `yield` teardown, scopes, and security schemes.
-   :material-rocket-launch: **[Deployment](../guide/deployment.md)** — run the
    app under a production ASGI server.

</div>
