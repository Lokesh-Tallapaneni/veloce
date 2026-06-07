---
description: Tutorial step 1 — write and run your first Veloce app, a single JSON handler, with the built-in development server.
tags: [tutorial, getting-started]
---

# 1. Your first app

We start with the smallest thing that runs: one handler that returns JSON.

Create a file called `tasks.py`:

```python title="tasks.py"
from veloce import Veloce

app = Veloce(title="Tasks API", version="1.0.0")


@app.get("/")
async def index():
    return {"message": "Tasks API is running"}


if __name__ == "__main__":
    app.run(port=8000)
```

Run it:

```bash
python tasks.py
```

Then open [http://localhost:8000/](http://localhost:8000/) — you should see:

```json
{"message": "Tasks API is running"}
```

Stop the server with `Ctrl+C`.

## What just happened

- `Veloce()` created the application. It **is** a router, so `app.get`,
  `app.post`, and the other HTTP verbs hang directly off it. `title` and
  `version` feed the generated OpenAPI document.
- `@app.get("/")` registered the `index` coroutine to handle `GET /`.
- Returning a `dict` produced a JSON response automatically — Veloce
  serialises it and sets `Content-Type: application/json` for you.
- `app.run()` started the built-in **development** server. For production you
  would run the app under an ASGI server such as `uvicorn tasks:app`.

!!! note "`async def` is preferred, `def` is supported"
    Define handlers with `async def` for best performance — they run directly
    on the event loop. A plain `def` handler also works: Veloce detects it and
    runs it in a thread-pool executor so it never blocks the loop. Reach for a
    sync handler when calling blocking library code you cannot `await`.

## Add a second route

A handler can return any JSON-serialisable value. Add a health check:

```python title="tasks.py"
from veloce import Veloce

app = Veloce(title="Tasks API", version="1.0.0")


@app.get("/")
async def index():
    return {"message": "Tasks API is running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(port=8000)
```

Restart the server and visit
[http://localhost:8000/health](http://localhost:8000/health).

## Next steps

So far the routes are static. Next we read values out of the URL:
**[Path & query parameters](path-and-query-params.md)**.
