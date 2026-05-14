# Veloce

Ultra-fast async Python web framework. ASGI-native, batteries included —
routing, dependency injection, OpenAPI, WebSockets, templating, sessions,
and a built-in test client, all in one tree.

## Install

```bash
pip install veloce
```

## Minimal example

```python
from veloce import Veloce, Request

app = Veloce()


@app.get("/")
async def index(request: Request):
    return {"hello": "world"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Run it:

```bash
python -m uvicorn main:app
# or
python main.py
```

## Why

Veloce is a from-scratch async framework, not a wrapper around an existing
one. The radix router, request/response pipeline, dependency injection,
OpenAPI generation, WebSocket handling, and test client are all in-tree.

- Built on raw `asyncio` (`uvloop` where available)
- `orjson` for JSON, `httptools` for parsing, `pydantic` v2 for validation
- Ergonomic helpers: `g`, `flash`, `make_response`, `jsonify`, `abort`
- Typed dependency injection: `Depends`, `Security`, `SecurityScopes`
- OpenAPI 3.1 auto-documentation with Swagger UI and ReDoc
- WebSockets, Server-Sent Events, background tasks, middleware, signed
  sessions, and signals — all in core

## Highlights

- Radix-tree router with typed path converters (`int`, `float`, `uuid`,
  `path`, custom)
- Per-request reflection eliminated from the hot path — handler
  signatures are inspected once at registration
- Pydantic-validated request bodies and `response_model` serialization
- Blueprints / sub-routers with nesting, prefixes, and scoped hooks
- In-memory `TestClient` that drives the real ASGI surface

## Docs

- Per-feature design notes: [`docs/design/`](docs/design/)
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)

## Status

Pre-1.0. The public API surface is stabilising; see the changelog for
release history.

## License

MIT.
