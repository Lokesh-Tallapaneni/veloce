# Design: `yield` dependencies with teardown (D4)

## Contract

A dependency function (sync or async) that uses `yield` instead of `return`
becomes a **context-managed dependency**:

- The code **before** `yield` is the setup; the value yielded is the
  dependency result that the handler receives.
- The code **after** `yield` is the teardown; it runs **after the response**
  has been produced, in **reverse order** of setup.
- Teardown runs whether the handler returned normally or raised. If an
  exception escaped the handler, it is `gen.throw()`-n into the dependency
  so `try/except/finally` blocks see it.

The design builds on PEP 342 (generator semantics), PEP 525 (async
generators), and the `contextlib.contextmanager` / `ExitStack` teardown
model: a dependency that `yield`s its value behaves like a context
manager scoped to the request.

## Observable behavior

```python
def db():
    session = make_session()
    try:
        yield session              # ← dependency result
    finally:
        session.close()            # ← runs after response

@app.get("/users")
async def list_users(s = Depends(db)):
    return s.execute("SELECT * FROM users").all()
```

Async generators work the same way:

```python
async def db():
    session = await make_async_session()
    try:
        yield session
    finally:
        await session.close()
```

Multiple yield dependencies tear down in **reverse** registration order:

```python
def outer(): events.append("setup"); yield; events.append("teardown")
def inner(): events.append("setup"); yield; events.append("teardown")

@app.get("/x")
async def h(o = Depends(outer), i = Depends(inner)): ...

# events on a request: outer-setup, inner-setup, inner-teardown, outer-teardown
```

## Data model

`_Slot` (`veloce._handler_plan`) gains two flags:
- `dep_is_gen: bool` — `inspect.isgeneratorfunction(dep)`
- `dep_is_async_gen: bool` — `inspect.isasyncgenfunction(dep)`

`DependencyResolver` gains:
- `_teardowns: list[tuple[str, Generator | AsyncGenerator]]` — per-request
  stack of live generators awaiting teardown. Cleared on each `resolve_plan`
  call (no leak across requests).
- `run_teardowns(exc: BaseException | None)` — drains the stack in reverse,
  advancing each generator one step (or throwing `exc` into it).

`Veloce._dispatch_request` calls `resolver.run_teardowns(_exc)` in its
`finally` block, **before** the `teardown_request` hooks.

## Threading model

The teardown stack is per-request, mutated only by the request task. No
locks. Free-threaded build: safe.

## Hot-path budget

- Cost added to a non-yield dependency: zero (`is_gen`/`is_async_gen`
  branch is False; existing code path unchanged).
- Cost added to a yield dependency: one `next(gen)` (sync) or
  `await gen.__anext__()` (async) on setup, one ditto on teardown, plus a
  push/pop on the per-request `_teardowns` list. All O(1).

## Error handling

- Generator that exits without yielding → `RuntimeError` from the
  resolver (surfaced as a 500 by the default error handler). This is a
  programming error in the dependency code, not a runtime condition.
- Exception inside teardown code → silently swallowed so the outer
  response cycle is unaffected. We log nothing here because the response
  is already on its way; teardown errors are write-only at this point.
  (Future enhancement: emit a logger warning.)

## Trade-offs

- **Single-yield only.** Generators that `yield` more than once are still
  set up (first yield) and torn down (advance past second yield will run
  the post-yield code). The dependency value is always the **first**
  yielded value.
- **No context-aware injection during teardown.** The teardown runs
  outside the request context; if it needs `request`, capture it during
  setup.
- **Override-friendly.** `app.dependency_overrides[db] = fake_db` works:
  if the override is itself a generator function, it follows the same
  teardown contract; if it's a regular function, it just returns a value.

## References

- PEP 342 (Coroutines via enhanced generators)
- PEP 525 (Asynchronous generators)
- `contextlib.ExitStack` (reverse teardown ordering)
