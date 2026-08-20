---
description: >-
  Freeze a Veloce app into one self-contained executable with PyInstaller —
  the no-uvicorn app.run() serving path, a --onefile build, the pydantic and
  veloce collect flags, and bundling your own templates and static assets.
tags: [pyinstaller, deployment, single-binary, packaging]
---

# Single-binary deploys

Veloce apps freeze cleanly into a single executable with
[PyInstaller](https://pyinstaller.org/), because production serving needs no
uvicorn subprocess: [`app.run()`](../reference/application.md#veloce.Veloce.run) drives the
built-in HTTP/1.1 and WebSocket server in-process, so the whole app is one
Python program with one entry point. This page builds a worked `--onefile`
binary.

!!! warning "Supported by design, not officially tested"
    Veloce ships **no** PyInstaller spec, hook, or CI job, and the freeze path
    is **not** part of the test suite. The framework is structured so that a
    freeze works — a static import graph and an in-process server with no
    dynamic self-imports on the request path — but treat this as a recipe you
    verify for your own app, not a tested, guaranteed-supported configuration.

## Why it freezes

PyInstaller traces a program's import graph statically and bundles every module
it finds. Two Veloce properties make that trace complete:

- **No mandatory ASGI server.** `app.run()` serves directly via the built-in
  server, so the binary needs only the interpreter, `veloceframework`, and your
  code — there is no `uvicorn module:app` subprocess to spawn, no second
  process to locate at runtime.
- **No dynamic self-imports on the serving path.** The native server imports
  its protocol statically, so PyInstaller's static analysis sees everything the
  request path uses without runtime `importlib` calls it cannot follow.

The freeze target is an ordinary full-program file that calls `app.run()`.

```python title="main.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index(request):
    return {"message": "Hello from a single binary"}


if __name__ == "__main__":
    app.run(port=8000)
```

This is the same `main.py` you would run with `python main.py`; freezing does
not change the entry point.

## Build the executable

Install PyInstaller alongside your app and build with `--onefile` to produce one
self-contained binary.

```bash
pip install veloceframework pyinstaller

pyinstaller --onefile \
    --collect-all pydantic \
    --collect-all pydantic_core \
    --collect-submodules veloce \
    main.py
```

The result is a single executable in `dist/` (`dist/main` on Linux/macOS,
`dist\main.exe` on Windows). Run it directly — no Python install, no virtualenv:

```bash
./dist/main
```

It starts the built-in server on port `8000`, exactly as `python main.py` would.

| Flag | Purpose |
| --- | --- |
| `--onefile` | Pack everything into one executable instead of a `dist/` folder. |
| `--collect-all pydantic` | Bundle Pydantic's data files and submodules so validation works frozen. |
| `--collect-all pydantic_core` | Bundle the compiled `pydantic_core` extension Pydantic v2 depends on. |
| `--collect-submodules veloce` | Pull in every `veloce.*` submodule, including ones reached only through framework dispatch. |
| `--add-data` | The argument is `SOURCE:DEST` (use `;` instead of `:` as the separator on Windows). |

!!! note "Why `--collect-submodules veloce`"
    PyInstaller follows static imports well, but Veloce reaches some modules
    through registry and dispatch indirection rather than a literal top-level
    `import`. `--collect-submodules veloce` forces every `veloce.*` submodule
    into the bundle so a code path you exercise at runtime is never missing.

!!! tip "Bind for the target host"
    `app.run()` defaults to `127.0.0.1`, reachable only from the local machine.
    On a server or in a container where the binary must answer external
    traffic, bind to all interfaces with `app.run(port=8000, bind_all=True)`.
    Passing both `host=...` and `bind_all=True` raises `ValueError`.

## Bundling templates and static files

PyInstaller bundles Python modules, not arbitrary data. If your app renders
Jinja templates or serves a static directory, those files are **not** picked up
automatically — declare each one with `--add-data` so it travels inside the
binary.

```bash
pyinstaller --onefile \
    --collect-all pydantic \
    --collect-all pydantic_core \
    --collect-submodules veloce \
    --add-data "templates:templates" \
    --add-data "static:static" \
    main.py
```

At runtime PyInstaller unpacks the bundle to a temporary
directory and exposes its path as `sys._MEIPASS`; resolve your asset directories
against it so they point inside the bundle when frozen and at the source tree
otherwise.

```python title="main.py" hl_lines="9 10"
import sys
from pathlib import Path

from veloce import Jinja2Templates, Request, Veloce

app = Veloce()

# When frozen, assets live under the unpacked bundle dir; otherwise alongside main.py.
base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount_static("/static", directory=str(base_dir / "static"))


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


if __name__ == "__main__":
    app.run(port=8000)
```

The same `base_dir` resolves correctly both ways: frozen, `sys._MEIPASS` is the
unpacked bundle; unfrozen, `getattr` falls back to the script's own directory.

!!! warning "Each data directory needs its own `--add-data`"
    A `--collect-*` flag bundles **code**; it does not sweep in your templates
    or static assets. Every directory your app reads at runtime needs an
    explicit `--add-data`, and your code must resolve that directory against
    `sys._MEIPASS` when frozen — otherwise the binary starts but `404`s or
    raises `TemplateNotFound` on the first request that touches an asset.

## If you freeze the uvicorn path instead

The freeze works because `app.run()` keeps everything in one process. If you
instead bundle a program that launches uvicorn, you inherit uvicorn's own
packaging requirements — its protocol and lifespan implementations are selected
by string at runtime, so PyInstaller cannot trace them and you must add them as
`--hidden-import` (or a uvicorn PyInstaller hook) yourself.

!!! tip "Prefer the native path for binaries"
    For a single-binary deploy, `app.run()` is the simpler target: one process,
    one static import graph, no runtime server-string resolution to chase down.
    Reach for uvicorn-in-a-binary only if you specifically need a uvicorn
    feature, and expect to maintain its `--hidden-import` list.

## Next steps

- [Docker](docker.md) — containerise the same `app.run()` program instead of freezing it.
- [Run a server manually](manually.md) — `app.run()`, `veloce run`, and uvicorn compared.
- [Native server deep dive](../surpass/native-server.md) — what the built-in server does and its hardening knobs.
- Full signatures are in the [API reference](../reference/index.md).
