---
description: Configure a Veloce app through app.config — load settings from objects, Python files, env vars, and structured files, with the built-in default keys explained.
tags: [config, settings, environment, dotenv]
---

# Configuration

Every Veloce application carries a configuration dictionary at `app.config`.
It is an ordinary mapping seeded with documented defaults, plus loader
methods that pull settings from Python objects, files, and the environment.

```python
from veloce import Veloce

app = Veloce()

app.config["DEBUG"] = True
app.config["SECRET_KEY"] = "change-me"

print(app.config["DEBUG"])          # True
print(app.config["PREFERRED_URL_SCHEME"])  # "http" (a built-in default)
```

`app.config` is an instance of `Config` (importable as
`from veloce.config import Config`), a `dict` subclass. Every dict idiom
keeps working — `config["KEY"] = value`, `config.get("KEY")`,
`config.update(...)`, `"KEY" in config`. The loader methods below add
convenient ways to populate it from external sources.

## Only uppercase keys are configuration

The loaders store a key only when its name is made of ASCII uppercase
letters, digits, and underscores, and does not start with a digit
(`DEBUG`, `SECRET_KEY`, `MAX_CONTENT_LENGTH`). Lowercase names in a source
module or mapping are treated as private and skipped. This lets a settings
module keep helper variables and imports alongside its config without
leaking them into `app.config`.

Direct assignment (`app.config["debug"] = True`) bypasses this filter — the
uppercase rule applies only to the `from_*` loaders.

## Built-in defaults

A fresh `app.config` is pre-populated so reads never raise `KeyError`.
The defaults are:

| Key | Default | Purpose |
| --- | --- | --- |
| `DEBUG` | `False` | Enable debug behaviour (tracebacks, propagation). |
| `TESTING` | `False` | Mark the app as under test. |
| `SECRET_KEY` | `None` | Key for signing sessions and tokens. |
| `SERVER_NAME` | `None` | Host (and optional port) used for URL building. |
| `PREFERRED_URL_SCHEME` | `"http"` | Scheme used when generating external URLs. |
| `MAX_CONTENT_LENGTH` | `104857600` (100 MiB) | Maximum request body size in bytes. The body is buffered in memory, so the default bounds a large-request OOM; raise it for large-upload endpoints, or set `None` for unlimited. |
| `MAX_FORM_PARTS` | `1000` | Maximum number of form parts (multipart) or fields (urlencoded). `None` lifts the cap for both. |
| `MAX_FORM_PART_SIZE` | `10485760` | Maximum size of a single form part in bytes (applies to both file and text parts unless overridden below). |
| `MAX_FORM_FILES` | `None` | Maximum number of file parts (`None` = only bounded by `MAX_FORM_PARTS`). |
| `MAX_FORM_FIELDS` | `None` | Maximum number of text-field parts (`None` = only bounded by `MAX_FORM_PARTS`). |
| `MAX_FORM_FILE_SIZE` | `None` | Per-file size limit in bytes; overrides `MAX_FORM_PART_SIZE` for file parts. |
| `MAX_FORM_FIELD_SIZE` | `None` | Per-text-field size limit in bytes; overrides `MAX_FORM_PART_SIZE` for text parts. |
| `MAX_FORM_FIELD_MEMORY` | `None` | Cumulative resident-memory ceiling (bytes) across all text fields, including field-name bytes. |
| `JSON_SORT_KEYS` | `False` | Sort keys when serialising JSON responses. |
| `JSONIFY_PRETTYPRINT_REGULAR` | `False` | Pretty-print JSON responses. |
| `PROPAGATE_EXCEPTIONS` | `None` | Re-raise unhandled exceptions out of dispatch. |
| `SEND_FILE_MAX_AGE_DEFAULT` | `None` | Default `Cache-Control: max-age` for `send_file` / `async_send_file` when the caller passes no `max_age=`. |
| `REQUEST_HANDLER_TIMEOUT` | `30` | Per-handler timeout in seconds, on the built-in serving path. Under an ASGI server the handler is not bounded by Veloce; use the server's own timeout. |
| `KEEP_ALIVE_TIMEOUT` | `75` | Keep-alive timeout in seconds, on the built-in serving path. |
| `REQUEST_TIMEOUT` | `30` | Request read timeout in seconds, on the built-in serving path. |
| `TCP_KEEPALIVE` | `True` | Enable OS-level TCP keepalive (`SO_KEEPALIVE`) on the built-in serving path. |
| `TCP_KEEPALIVE_IDLE` | `None` | Idle seconds before the first keepalive probe (`TCP_KEEPIDLE`); Linux/macOS only. |
| `TCP_KEEPALIVE_INTERVAL` | `None` | Seconds between keepalive probes (`TCP_KEEPINTVL`); Linux only. |
| `TCP_KEEPALIVE_COUNT` | `None` | Failed probes before the connection is dropped (`TCP_KEEPCNT`); Linux only. |
| `JSON_ERRORS_VERBOSE` | `False` | Include parser detail in JSON body-parse error responses. |
| `GRACEFUL_TASK_TIMEOUT` | `10` | Seconds to wait for background tasks to finish cancelling on shutdown. |
| `GRACEFUL_DRAIN_TIMEOUT` | `30` | Seconds shutdown waits for in-flight requests to finish. Runs before `GRACEFUL_TASK_TIMEOUT`, so a container's termination grace period must cover both. |
| `WEBSOCKET_IDLE_TIMEOUT` | `None` | Seconds a WebSocket may sit idle before it is closed with `1001 Going Away`. `None` disables it. A handler's `set_idle_timeout()` overrides it. |

A few of these keys drive framework behaviour directly. `MAX_CONTENT_LENGTH`
bounds the request body the server will read. `PROPAGATE_EXCEPTIONS`
controls whether unhandled exceptions re-raise: when it is left at `None`,
exceptions propagate only if both `DEBUG` and `TESTING` are `True` (useful
so test runs surface the real traceback). Boolean keys loaded from an env
file arrive as strings and are coerced (`"false"`, `"0"`, `"off"` read as
off). `JSON_SORT_KEYS` orders the keys of every JSON response, whatever the
handler returned - a `dict`, a `list`, a model, or `jsonify(...)`.

`SECRET_KEY` is the one key the session middlewares read: a middleware
constructed without `secret_key=` signs with it. Every **cookie** setting -
name, path, `Secure`, `HttpOnly`, `SameSite`, lifetime - comes from the
middleware's own constructor and from nowhere else, so
`app.secret_key = "..."` plus a bare `app.add_middleware(SessionMiddleware)`
is a complete setup. Explicit constructor arguments always win over config.

!!! warning "Set a real SECRET_KEY before using sessions"
    `SECRET_KEY` defaults to `None`. Signed sessions, flash messages, and
    the [`Signer`](../reference/security.md#veloce.Signer) require a non-empty,
    unpredictable key. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`
    and load it from the environment, never hard-code it in source.

## Loading from a Python object

`from_object` imports every uppercase attribute of a module, class, or
instance. Pass the object itself, or a dotted import path as a string.

```python
import types

from veloce import Veloce

settings = types.SimpleNamespace(
    DEBUG=True,
    SECRET_KEY="from-object",
    internal_note="ignored — lowercase",
)

app = Veloce()
app.config.from_object(settings)

print(app.config["DEBUG"])       # True
print("internal_note" in app.config)  # False
```

A dotted string is resolved before its attributes are read, which makes it
easy to select an environment-specific class:

```python
from veloce import Veloce

app = Veloce()
# Resolves the dotted path, then copies its UPPERCASE attributes.
app.config.from_object("myapp.settings.Production")
```

The common pattern is one module per environment, or one class per
environment in a shared module, selected at startup from an env var.

## Loading from a Python file

`from_pyfile` executes a standalone `.py` file and copies its uppercase
module-level names. The file does not need to be importable as a package.

```python
from pathlib import Path

from veloce import Veloce

Path("production.cfg").write_text(
    "DEBUG = False\n"
    "SECRET_KEY = 'loaded-from-file'\n"
    "MAX_CONTENT_LENGTH = 5 * 1024 * 1024\n"
)

app = Veloce()
app.config.from_pyfile("production.cfg")

print(app.config["MAX_CONTENT_LENGTH"])  # 5242880
```

Pass `silent=True` to treat a missing file as a no-op (returns `False`)
instead of raising `OSError`. Errors raised *inside* the file always
propagate — a typo in a settings file is a real bug, not something to
swallow.

!!! warning "from_pyfile executes arbitrary code"
    `from_pyfile` runs the file with `exec`. Only point it at files you
    control. Never load a config file from an untrusted or user-supplied
    path.

`from_envvar` combines the two: it reads a filename from a named
environment variable and feeds it to `from_pyfile`.

```python
import os

from veloce import Veloce

os.environ["MYAPP_SETTINGS"] = "production.cfg"

app = Veloce()
app.config.from_envvar("MYAPP_SETTINGS")
```

## Loading from the environment

`from_prefixed_env` pulls every environment variable that begins with a
prefix (default `VELOCE`), strips the `<prefix>_` part, and stores the
remainder. Values are JSON-decoded, falling back to the raw string when
JSON parsing fails — so `VELOCE_DEBUG=true` becomes the boolean `True`,
while `VELOCE_SECRET_KEY=abc` stays the string `"abc"`.

```python
import os

from veloce import Veloce

os.environ["VELOCE_DEBUG"] = "true"          # JSON -> True
os.environ["VELOCE_SECRET_KEY"] = "s3cret"   # not JSON -> "s3cret"
os.environ["VELOCE_MAX_CONTENT_LENGTH"] = "1048576"  # JSON -> 1048576

app = Veloce()
app.config.from_prefixed_env()

print(app.config["DEBUG"])               # True
print(app.config["SECRET_KEY"])          # "s3cret"
print(app.config["MAX_CONTENT_LENGTH"])  # 1048576
```

A double underscore in the variable name builds nested configuration:
`VELOCE_MAIL__SERVER` sets `app.config["MAIL"]["SERVER"]`.

```python
import os

from veloce import Veloce

os.environ["VELOCE_MAIL__SERVER"] = "smtp.example.com"
os.environ["VELOCE_MAIL__PORT"] = "587"

app = Veloce()
app.config.from_prefixed_env()

print(app.config["MAIL"])  # {'SERVER': 'smtp.example.com', 'PORT': 587}
```

Pass a custom `prefix` to read a different namespace, and a custom `loads`
callable to change how values are decoded.

## Loading from a dotenv file

`from_env_file` reads `KEY=VALUE` pairs from a `.env`-style file. It skips
blank lines and `#` comment lines, accepts an optional `export ` prefix,
and unquotes values wrapped in matching single or double quotes. Values
are always stored as strings — a `.env` file carries no types.

```python
from pathlib import Path

from veloce import Veloce

Path(".env").write_text(
    "# application settings\n"
    "SECRET_KEY='dotenv-secret'\n"
    "export SERVER_NAME=example.com\n"
)

app = Veloce()
app.config.from_env_file()  # defaults to ".env"

print(app.config["SECRET_KEY"])   # "dotenv-secret"
print(app.config["SERVER_NAME"])  # "example.com"
```

!!! note
    `from_env_file` keeps values as raw strings. When you need typed values
    (booleans, integers, nested mappings) from the environment, prefer
    `from_prefixed_env`, which JSON-decodes each value.

As with the other loaders, `silent=True` turns a missing file into a
`False` return instead of an exception.

## Loading from a structured file

`from_file` handles any structured format by accepting a `load` callable
that takes an open file object and returns a mapping. The default loader
parses JSON. Pass `text=True` to open the file in text mode for loaders
that expect a string (most do); the default binary mode suits JSON and
TOML.

```python
import tomllib
from pathlib import Path

from veloce import Veloce

Path("config.toml").write_text(
    'SECRET_KEY = "from-toml"\n'
    "MAX_FORM_PARTS = 50\n"
)

app = Veloce()
app.config.from_file("config.toml", load=tomllib.load)

print(app.config["MAX_FORM_PARTS"])  # 50
```

The returned object must be a mapping; anything else raises `TypeError`.

## Bulk updates and namespaces

`from_mapping` applies a mapping and/or keyword arguments in one call,
respecting the uppercase rule:

```python
from veloce import Veloce

app = Veloce()
app.config.from_mapping(
    {"DEBUG": False},
    SECRET_KEY="from-mapping",
    PREFERRED_URL_SCHEME="https",
)
```

`get_namespace` extracts every key that shares a prefix, which is handy
for passing one subsystem's settings to an extension. By default the
prefix is trimmed and the remaining key is lower-cased:

```python
from veloce import Veloce

app = Veloce()
app.config.from_mapping(
    MAIL_SERVER="smtp.example.com",
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
)

mail = app.config.get_namespace("MAIL_")
print(mail)  # {'server': 'smtp.example.com', 'port': 587, 'use_tls': True}
```

Pass `lowercase=False` to keep the original case, or `trim_namespace=False`
to keep the prefix on each returned key.

## A typical startup

The loaders compose. A common arrangement loads a base object, overlays an
environment file, then lets real environment variables win:

```python
import types

from veloce import Veloce

base = types.SimpleNamespace(DEBUG=False, JSON_SORT_KEYS=True)

app = Veloce()
app.config.from_object(base)            # defaults from a base object
app.config.from_env_file(silent=True)   # overlay a local .env if present
app.config.from_prefixed_env()          # environment wins last
```

Later loaders overwrite earlier values for the same key, so order is the
precedence order.

## Next steps

- [Error handling](error-handling.md) — turn configuration like
  `PROPAGATE_EXCEPTIONS` into the right behaviour for failed requests, and
  register custom error responses.
- [Sessions](sessions.md) and [Middleware](middleware.md) — the
  `SECRET_KEY` drives session signing; cookie
  attributes.
- The [API reference](../reference/index.md) documents the public `veloce`
  surface; `Config` and its loader methods live in `veloce.config`.
