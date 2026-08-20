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
