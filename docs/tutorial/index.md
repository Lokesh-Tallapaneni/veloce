---
description: A step-by-step Veloce tutorial — build one small Tasks API from a hello-world handler up to a tested, validated, dependency-injected app.
tags: [tutorial, getting-started, beginner]
---

# Tutorial

This tutorial builds **one application from scratch**, a small *Tasks API*,
adding a single concept per step. By the end you will have a typed,
validated, dependency-injected service with a test suite — and you will have
seen every core piece of Veloce in the order you would reach for it.

Each step starts from the app you had at the end of the previous one, shows
the complete file (not a fragment), and ends with a *Next steps* link. You can
copy any code block and run it as-is.

## What we'll build

A Tasks API that grows feature by feature:

1. **[Your first app](first-app.md)** — a running "hello world" handler.
2. **[Path & query parameters](path-and-query-params.md)** — read typed
   values from the URL.
3. **[Request bodies](request-body.md)** — accept and validate JSON with
   Pydantic models.
4. **[Dependencies](dependencies.md)** — share a data store across handlers
   with `Depends`.
5. **[Errors & validation](errors-and-validation.md)** — return clean error
   responses.
6. **[Testing](testing.md)** — drive the whole app with the in-memory
   `TestClient`.

## Before you start

Veloce needs **Python 3.10 or newer**. Install it from PyPI:

```bash
pip install veloceframework
```

That is the only dependency you need for this tutorial — Pydantic comes with
Veloce, and the built-in development server needs nothing extra.

!!! tip "Already know FastAPI or Flask?"
    Veloce's surface will feel familiar — `app.get(...)`, `Depends(...)`,
    Pydantic bodies — but it is a custom framework, not a wrapper. Read the
    code in each step rather than assuming behaviour from another framework.

## Next steps

Start building: **[Your first app](first-app.md)**.
