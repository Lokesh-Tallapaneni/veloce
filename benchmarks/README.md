# Benchmarks

Continuous performance benchmarks for Veloce, run on
[CodSpeed](https://codspeed.io) through
[`pytest-codspeed`](https://github.com/CodSpeedHQ/pytest-codspeed).

They live outside `tests/` on purpose: `[tool.pytest.ini_options].testpaths`
points at `tests`, so a plain `pytest` never collects them and the normal test
matrix stays fast. CI runs them in a dedicated `codspeed` job
(`.github/workflows/codspeed.yml`) on every push to `main` and every pull
request, and CodSpeed reports the delta against the base commit.

## Running locally

```bash
uv sync --all-extras --dev

# Quick check that every benchmark still executes (no measurement).
uv run pytest benchmarks -q

# Measured run, walltime on the local machine.
uv run pytest benchmarks --codspeed
```

## What is covered

| File                       | Area                                                          |
| -------------------------- | ------------------------------------------------------------- |
| `test_routing.py`          | Radix-tree matching, typed converters, `url_for` reverse       |
| `test_dispatch.py`         | Full `handle_request` path: DI, validation, middleware chain   |
| `test_serialization.py`    | `jsonable_encoder`, `jsonify`, JSON response rendering         |
| `test_http_primitives.py`  | Request/response construction, headers, cookies, query strings |
| `test_asgi.py`             | End-to-end ASGI round trips through the in-memory test client  |

## Adding a benchmark

Benchmarked work must be a single, self-contained unit — build the app, the
request, and any payload *outside* the measured callable so the numbers track
the code path under test rather than fixture setup. Async code paths go through
the `run_async` helper in `conftest.py`, which drives one module-scoped event
loop instead of paying `asyncio.run` setup on every iteration.

## Measurement modes

The suite runs under two CodSpeed instruments, and which one a benchmark
belongs to is a property of what it measures.

**CPU simulation** (`benchmarks/`, excluding `walltime/`) counts instructions
under Valgrind. It is deterministic and has no noise floor, so a few percent
regression is visible - but it cannot instrument system calls, and silently
excludes them from the reported figure.

**Wall time** (`benchmarks/walltime/`) measures elapsed time. Everything there
crosses the thread-pool boundary: a plain `def` handler, which Veloce offloads
so it cannot block the event loop, and `GZipMiddleware`, which offloads
compression. Under simulation those benchmarks report a number that omits
their dominant cost - CodSpeed flagged the gzip one as "2.7 ms" while noting
that 32 system calls worth 257.5 ms had been left out.

**Memory** (`benchmarks/memory/`) measures allocation rather than speed, so it
covers what the framework *holds*: the compiled route table, a deep dependency
graph, the OpenAPI schema, and the per-connection objects a server keeps alive
for as long as a client is connected. `WebSocket` and `Request` are slotted to
keep those small; an attribute added without a matching `__slots__` entry
restores the per-instance `__dict__` and undoes it - invisible to both timing
instruments, a step change here.

Run them separately, the way CI does:

```bash
uv run pytest benchmarks --ignore=benchmarks/walltime --ignore=benchmarks/memory --codspeed
uv run pytest benchmarks/walltime --codspeed
uv run pytest benchmarks/memory --codspeed
```

Choosing a suite is a property of what the benchmark measures, not a
preference. A benchmark that crosses a thread boundary belongs in `walltime/`:
in the simulation suite it produces a figure that looks stable while the real
cost moves underneath it. One that measures a structure's footprint belongs in
`memory/`, where the instrument can see it.

Beware of memoised work. `app.openapi()` caches onto `app.openapi_schema`, so a
benchmark that calls it repeatedly measures an attribute read - it reported
`0ns` over six million iterations before the cache reset was added. Clear any
cache inside the benchmarked callable.
