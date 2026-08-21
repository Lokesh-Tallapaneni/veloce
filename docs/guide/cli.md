---
description: The veloce command-line interface — scaffold projects, generate boilerplate, run the server, inspect routes, and open a shell.
tags: [cli, scaffolding, tooling]
---

# Command-Line Interface

Installing Veloce puts a `veloce` command on your `PATH`. It scaffolds new
projects, generates boilerplate files, serves an app, prints its routes, and
drops into a REPL. The CLI is built on the standard library, so it adds no
runtime dependencies.

```bash
veloce --help
veloce --version
```

## Scaffold a project: `veloce new`

Create a new project from a template:

```bash
veloce new myapp --template api
cd myapp
uv run veloce run app:app --reload
```

Three templates are available:

| Template | Generates |
|----------|-----------|
| `minimal` (default) | A single-route app, a test, and project config. |
| `api` | Pydantic models, a router blueprint, dependency injection, and OpenAPI docs. |
| `web` | Jinja templates, static files, and a server-rendered page. |

Every generated project imports the public `veloce` API and passes `ruff` and
`mypy` out of the box. The layout for `--template api` is:

```text
myapp/
├── app.py            # creates the app, registers the blueprint
├── api.py            # the router blueprint + dependency injection
├── models.py         # Pydantic models
├── pyproject.toml    # dependencies + ruff/mypy/pytest config
├── README.md
├── .gitignore
└── tests/
    └── test_app.py
```

!!! tip
    Pass `--dir PATH` to create the project somewhere other than the current
    directory, and `--force` to write into a directory that already exists.

## Generate a file: `veloce generate`

Emit a single boilerplate file (alias: `veloce g`):

```bash
veloce generate model product
veloce g blueprint orders
```

| Kind | Produces |
|------|----------|
| `route` | A blueprint with one route. |
| `blueprint` | A blueprint with multiple routes. |
| `middleware` | A `Middleware` subclass. |
| `model` | A Pydantic model. |
| `security` | An API-key security dependency. |

The file is written as `<name>.py` in the current directory. Use `--stdout` to
print it instead of writing, and `--force` to overwrite an existing file:

```bash
veloce generate security api_key --stdout
```

## Run the server: `veloce run`

Serve an app referenced as `module:attribute` (the same form ASGI servers use):

```bash
veloce run app:app --host 0.0.0.0 --port 8000 --reload
```

`veloce run` uses [uvicorn](https://www.uvicorn.org/) when it is installed (the
`veloceframework[uvicorn]` extra) and falls back to the built-in development
server otherwise. `--reload` works on either path — uvicorn's reloader, or the
built-in one (a stdlib file watcher, no extra dependency). `--workers` requires
uvicorn; the built-in server runs a single process.

!!! tip "Auto-reload during development"
    `veloce run app:app --reload` restarts the server whenever a project `.py`
    file changes. The same behaviour is available programmatically with
    `app.run(reload=True)`. The file-watching runs in a supervisor process, so
    the served app itself carries no overhead — keep `--reload` off in
    production.

!!! note
    A dotenv file named `.env` in the working directory is loaded automatically
    before the app imports. Pass `--env-file PATH` to choose another file or
    `--no-env-file` to skip it.

## Inspect routes: `veloce routes`

Print the registered route table — handy for checking a blueprint mount without
issuing requests:

```bash
veloce routes app:app
```

## Serve MCP: `veloce mcp`

An MCP client launches its servers from a config file naming a command and its
arguments, so this is what goes there — no wrapper script whose only job is to
call `mount_mcp`:

```json
{
  "mcpServers": {
    "inventory": {
      "command": "veloce",
      "args": ["mcp", "run", "app:app"]
    }
  }
}
```

`stdio` is the default because that is the form a client launches. The process
speaks JSON-RPC on stdout, so nothing else is written there — every message the
command emits goes to stderr.

Serve the same tools over HTTP instead with `--transport http`:

```bash
veloce mcp run app:app --transport http --port 8000 --path /mcp
veloce mcp run app:app --transport http --sessions   # Mcp-Session-Id lifecycle
```

`veloce mcp list` is the MCP counterpart to `veloce routes` — it answers "did my
tool register, and under what name" without launching a client to ask:

```bash
veloce mcp list app:app
```

```
TOOLS
  stock_level  Count the units on hand for a part
  reserve      Reserve units of a part

RESOURCES
  part://{part}  One part record

PROMPTS
  restock_note  Draft a restock request
```

See the [MCP guide](mcp.md) for what those primitives are and how to declare them.

!!! note "Added in version 0.16"
    `veloce mcp run` and `veloce mcp list`.

## Other commands

```bash
veloce check app:app    # run a pre-deploy security audit
veloce shell app:app    # a Python REPL with `app` and `g` loaded
veloce custom app:app -- <command>   # run an app.cli (Click) command
```

!!! note "`app.cli` needs the `cli` extra"
    The built-in `veloce` commands are pure stdlib `argparse` and need nothing
    extra. Flask-style per-app commands (`@app.cli.command()`, run via
    `veloce custom`, and `app.test_cli_runner()`) use Click, so install it with
    `pip install veloceframework[cli]`.

`veloce shell` runs inside an application context, so `current_app`, `g`, and
anything registered with `@app.shell_context_processor` resolve as they would in
a request.

## Adding your own commands

A distribution can add a subcommand by advertising a `veloce.commands` entry
point in its `pyproject.toml`:

```toml
[project.entry-points."veloce.commands"]
deploy = "mypkg.cli:register"
```

`mypkg.cli:register` is a callable that receives the argparse subparsers action
and adds one parser. Discovery is lazy — a plugin is imported only when its
command is the one selected, so built-in commands never run plugin code.

## What's next

- [Deployment](deployment.md) — serve the app you scaffolded under a production server.
- [Testing](testing.md) — the in-memory `TestClient` the generated tests use.
- [Configuration](configuration.md) — environment-driven settings and the `.env` file `veloce run` loads.
