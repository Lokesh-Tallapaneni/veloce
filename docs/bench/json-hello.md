# Bench result — json-hello

Identical handler in each framework: `GET /` returning the dict
`{"hello": "world"}`. This stresses the core dispatch + JSON-response
path with no path parameters, no body parsing, no middleware.

## Setup

- 5 runs per framework, 5 second timed window per run (after a 1 s
  warmup), 8 concurrent connections, run order shuffled.
- Server: `uvicorn --loop asyncio --no-access-log --log-level critical`,
  single worker.
- Client: `httpx.AsyncClient`, same machine, localhost.
- Versions: Veloce 0.1.0 · FastAPI 0.115.12 · Flask 3.1.1 · uvicorn 0.34.2
  · httpx 0.28.1 · CPython 3.13 on Windows 10.

## Result (seed 42, 5 measured runs + 1 discarded cold-cache round)

| framework | rps (med) | p50 µs | p99 µs | errors |
|-----------|----------:|-------:|-------:|-------:|
| **veloce**  | **1,203** | 6,352  | 9,508  | 0 |
| fastapi   | 1,178     | 6,444  | 10,419 | 0 |
| flask     | 766       | **5,121**  | **9,393**  | 0 |

**vs FastAPI:** Veloce wins on rps (+2 %), p50 (-1 %), and p99 (-9 %).

**vs Flask:** Veloce wins on rps (+57 %); Flask wins p50 (-19 %) and p99
(-1 %). Sync WSGI through `asgiref.WsgiToAsgi` keeps the per-request
critical path short and avoids event-loop scheduling gaps that show up
at the 99th percentile.

See the [README caveats](README.md#caveats--read-before-quoting-any-number-out-of-context):
Flask runs via `asgiref.WsgiToAsgi` under one uvicorn worker, which is
not its native deployment. These numbers are not "Veloce vs Flask in
production"; they are relative measurements within this harness.

## How to reproduce

```bash
python -m bench.comparative.bench json-hello --runs 5 --duration 5 --concurrency 8 --seed 42
```

Recorded numbers above are from a seeded run; the harness now prints the
seed it used so any future run can be byte-reproduced.
